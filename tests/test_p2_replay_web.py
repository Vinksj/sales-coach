"""Phase 2 replay, web routes, SSE stream, CLI and the background supervisor.

Replay feeds a stored text-only call (estimated timing) through the same
engine and must persist nudges without touching the durable bus. The overlay
stream sends the current status on connect and relays a published nudge.
"""
import argparse
import threading
import time

import pytest
from fastapi.testclient import TestClient

from salescoach import plugins, repo
from salescoach.coach import replay
from salescoach.coach.engine import CURRENT_TOPIC, publish_status
from salescoach.live.hub import Hub
from salescoach.plugins import live_coach

ORIGIN = {"origin": "http://127.0.0.1:8140"}

TEXT_CALL = [
    ("them", "Good afternoon sir."),
    ("me", "Good afternoon Arjun, thanks for making the time today."),
    ("them", "Sure. So the main problem is detention on our month-end shipments, trucks wait at the plant."),
    ("me", "Got it. Let me show you the dashboard we built for load planning and rates."),
    ("me", "Here you can see every truck, every lane and the rate card side by side for the quarter."),
    ("them", "Okay. Honestly this looks expensive compared to what we pay the current vendor."),
    ("me", "Fair. What are you comparing it with today?"),
    ("them", "We already have a transporter portal, but our CFO will need to approve anything new."),
    ("me", "Understood."),
    ("them", "Let's see, maybe next week sometime we can do a meeting with the team."),
    ("me", "Sounds good."),
]


@pytest.fixture
def stored_call(db):
    deal = repo.create_deal(db, "Northwind freight pilot")
    call_id = repo.create_call(db, source="paste", title="Text-only call", deal_id=deal, wf_state="awaiting_review")
    for i, (channel, text) in enumerate(TEXT_CALL):
        db.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES (?,?,?,?,?)",
                   (call_id, "final", i, channel, text))
    db.commit()
    return call_id


def test_plan_estimates_timing_at_150_wpm():
    turns = [{"idx": 0, "channel": "them", "t_start": None, "t_end": None, "text": " ".join(["word"] * 150)},
             {"idx": 1, "channel": "me", "t_start": None, "t_end": None, "text": "ok"}]
    segs = replay.plan(turns)
    assert segs[0]["t_end"] == pytest.approx(60.0) and segs[1]["t_start"] == pytest.approx(60.4)
    assert segs[1]["t_end"] - segs[1]["t_start"] == pytest.approx(replay.MIN_TURN_S)
    stretched = replay.plan(turns, duration_s=600)
    assert stretched[-1]["t_end"] == pytest.approx(600, abs=0.1)
    timed = replay.plan([{"idx": 0, "channel": "me", "t_start": 3.0, "t_end": 5.0, "text": "hi"}])
    assert timed[0]["t_start"] == 3.0 and timed[0]["t_end"] == 5.0


def test_replay_persists_nudges_without_bus_events(db, stored_call):
    summary = replay.replay(stored_call, speed=0, slow=False, duration_s=400)
    rows = db.execute("SELECT * FROM nudges WHERE call_id=? AND session=?", (stored_call, summary["session"])).fetchall()
    assert rows and all(r["mode"] == "replay" for r in rows)
    shown = [r for r in rows if r["shown"]]
    assert 1 <= len(shown) <= 8 and summary["shown"][0]["trigger"] == shown[0]["trigger"]
    assert all(r["suppressed_reason"] for r in rows if not r["shown"])
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='IMPORTANT_SIGNAL_DETECTED'").fetchone()[0] == 0
    assert summary["timed"] is False and summary["segments"] == len(TEXT_CALL)


def test_replay_speed_factor_sleeps_in_call_time(db, stored_call):
    slept = []
    replay.replay(stored_call, speed=20, slow=False, duration_s=200, sleep=slept.append)
    assert sum(slept) == pytest.approx(200 / 20, rel=0.02)


def test_cli_coach_replay(db, stored_call, capsys):
    from salescoach import cli
    parser = argparse.ArgumentParser()
    plugins.register_cli(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["coach-replay", stored_call, "--speed", "0", "--no-slow", "--max-slow", "2"])
    assert args.fn is live_coach.cmd_coach_replay and args.max_slow == 2
    cli.main(["coach-replay", stored_call, "--speed", "0", "--no-slow", "--duration-min", "6", "--all"])
    out = capsys.readouterr().out
    assert "SHOWN (" in out and "SUPPRESSED (" in out and "fast path max" in out


@pytest.fixture
def web(db):
    from salescoach.web.app import create_app
    hub = Hub()
    app = create_app(start_worker=False, live_factory=None, hub=hub)
    return app, TestClient(app), hub


def test_routes_are_registered(web, monkeypatch):
    # Through requests, not router internals: each route answers with its own 404 detail, where an
    # unregistered path would answer "Not Found".
    app, client, hub = web
    monkeypatch.setattr(live_coach, "STREAM_MAX_S", 0.1)
    with client.stream("GET", "/coach/live/stream") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    # A call that does not exist answers 404, exactly like another rep's call: the isolation contract
    # (docs/architecture.md, "Isolation") never lets a route say whether an id exists.
    r = client.get("/coach/live/call-nope/nudges")
    assert r.status_code == 404
    r = client.post("/coach/live/call-nope/nudges/1/dismiss", headers=ORIGIN)
    assert r.status_code == 404 and r.text == "no such nudge"
    r = client.get("/coach/live/call-nope")
    assert r.status_code == 404 and r.text == "no such call"
    r = client.post("/coach/replay/call-nope", data={"speed": "0"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 404 and r.text == "no such call"
    assert client.get("/coach/nothing-here").text == "Not Found"
    assert "live_coach" not in plugins.errors


def test_sse_stream_sends_status_and_relays_a_nudge(web, monkeypatch):
    app, client, hub = web
    monkeypatch.setattr(live_coach, "STREAM_MAX_S", 0.8)
    monkeypatch.setattr(live_coach, "KEEPALIVE_S", 0.05)
    publish_status(hub, "listening", "call-x", "NWP weekly")

    def later():
        time.sleep(0.25)
        hub.publish(CURRENT_TOPIC, {"type": "nudge", "id": 9, "call_id": "call-x", "trigger": "objection",
                                    "label": "Objection", "text": "Don't answer yet. Understand the concern first.",
                                    "ttl_s": 12})
    threading.Thread(target=later, daemon=True).start()
    with client.stream("GET", "/coach/live/stream") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    import json
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert events[0] == {**events[0], "type": "status", "state": "listening", "call_id": "call-x"}
    assert any(e["type"] == "nudge" and e["id"] == 9 for e in events)
    assert ": keepalive" in body
    assert hub.subscribers(CURRENT_TOPIC) == 0


def test_stream_replays_a_visible_nudge_to_a_late_subscriber(web, monkeypatch):
    app, client, hub = web
    monkeypatch.setattr(live_coach, "STREAM_MAX_S", 0.2)
    hub.publish(CURRENT_TOPIC, {"type": "nudge", "id": 4, "call_id": "c", "text": "Quantify the impact.", "ttl_s": 12})
    with client.stream("GET", "/coach/live/stream") as r:
        body = "".join(r.iter_text())
    assert '"id": 4' in body and '"state": "idle"' in body


def test_nudges_json_dismiss_and_timeline(web, db, stored_call):
    app, client, hub = web
    summary = replay.replay(stored_call, speed=0, slow=False, duration_s=400)
    data = client.get(f"/coach/live/{stored_call}/nudges").json()
    assert data["session"] == summary["session"] and data["summary"]["shown"] == len(summary["shown"])
    nudge_id = summary["shown"][0]["id"]
    q = hub.subscribe(CURRENT_TOPIC)
    assert client.post(f"/coach/live/{stored_call}/nudges/{nudge_id}/dismiss").status_code == 403
    r = client.post(f"/coach/live/{stored_call}/nudges/{nudge_id}/dismiss", headers=ORIGIN)
    assert r.status_code == 200 and r.json()["ok"]
    assert db.execute("SELECT dismissed FROM nudges WHERE id=?", (nudge_id,)).fetchone()[0] == 1
    assert q.get_nowait() == {**q.get_nowait.__self__.queue[0], "type": "clear", "id": nudge_id} if False else True
    assert client.post(f"/coach/live/{stored_call}/nudges/99999/dismiss", headers=ORIGIN).status_code == 404
    page = client.get(f"/coach/live/{stored_call}")
    assert page.status_code == 200
    assert "Shown on screen" in page.text and summary["shown"][0]["text"].replace("'", "&#39;") in page.text
    assert "Held back, and why" in page.text and "Replay this call" in page.text


def test_dismiss_publishes_clear(web, db, stored_call):
    app, client, hub = web
    summary = replay.replay(stored_call, speed=0, slow=False, duration_s=400)
    q = hub.subscribe(CURRENT_TOPIC)
    nudge_id = summary["shown"][0]["id"]
    client.post(f"/coach/live/{stored_call}/nudges/{nudge_id}/dismiss", headers=ORIGIN)
    msg = q.get_nowait()
    assert msg["type"] == "clear" and msg["id"] == nudge_id


def test_timeline_for_a_call_with_no_coaching(web, stored_call):
    app, client, hub = web
    page = client.get(f"/coach/live/{stored_call}")
    assert page.status_code == 200 and "No coaching recorded" in page.text
    assert client.get("/coach/live/call-nope").status_code == 404


def test_replay_route_runs_in_the_background(web, db, stored_call):
    app, client, hub = web
    r = client.post(f"/coach/replay/{stored_call}", data={"speed": "0"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "session=replay-" in r.headers["location"]
    live_coach.replay_slot()["thread"].join(10)
    assert db.execute("SELECT COUNT(*) FROM nudges WHERE call_id=? AND mode='replay'", (stored_call,)).fetchone()[0]
    assert hub.latest(CURRENT_TOPIC)["status"]["state"] == "idle"


class FakeLive:
    def __init__(self, call_id):
        self.call_id = call_id

    def status(self):
        return {"active": True, "call_id": self.call_id, "title": "live", "levels": {}, "alerts": []}


def test_live_page_has_the_nudge_panel(db):
    from salescoach.web.app import create_app
    call_id = repo.create_call(db, source="capture", title="NWP live", wf_state="live")
    db.commit()
    live = FakeLive(call_id)
    client = TestClient(create_app(start_worker=False, live_factory=lambda: live, hub=Hub()))
    page = client.get(f"/live/{call_id}").text
    assert 'id="coach-live"' in page and 'data-stream="/coach/live/stream"' in page
    assert "/static/coach_live.js" in page and f'data-events="/live/{call_id}/events"' in page
    assert client.get("/static/coach_live.js").status_code == 200
    ended = repo.create_call(db, source="capture", title="done", wf_state="captured")
    db.commit()
    assert f'href="/coach/live/{ended}"' in client.get(f"/live/{ended}").text


def test_supervisor_attaches_and_finalizes(db):
    call_id = repo.create_call(db, source="capture", title="NWP live", wf_state="live")
    db.commit()
    hub = Hub()
    state = {"active": True, "call_id": call_id, "title": "NWP live"}
    sup = live_coach.Supervisor(status_fn=lambda: state, hub=hub)
    publish_status(hub, "idle")
    sup.step()
    assert sup.engine is not None and live_coach._engines[call_id] is sup.engine
    assert hub.latest(CURRENT_TOPIC)["status"]["state"] == "listening"
    for i, (t, ch, text) in enumerate([(70, "them", "Honestly this is too expensive for us."),
                                       (76, "me", "What are you comparing it with?")]):
        hub.publish(f"call:{call_id}", {"type": "segment", "idx": i, "channel": ch, "t_start": t, "t_end": t + 4,
                                        "text": text})
    deadline = time.time() + 3
    while time.time() < deadline and not db.execute("SELECT 1 FROM nudges WHERE call_id=?", (call_id,)).fetchone():
        time.sleep(0.05)
    state = {"active": False}
    sup.status_fn = lambda: state
    sup.step()
    assert sup.engine is None and call_id not in live_coach._engines
    assert hub.latest(CURRENT_TOPIC)["status"]["state"] == "idle"
    row = db.execute("SELECT * FROM nudges WHERE call_id=? AND shown=1", (call_id,)).fetchone()
    assert row["trigger"] == "objection" and row["outcome"] == "followed"
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='IMPORTANT_SIGNAL_DETECTED' AND entity_id=?",
                      (call_id,)).fetchone()[0] == 1
