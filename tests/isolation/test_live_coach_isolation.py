"""The live-coach overlay topic is per user (Postgres, cloud mode).

The hub is process-wide. POST /coach/replay/{call_id} publishes the replay's status (call id and title)
and its nudges (text from the transcript) for the overlay, and GET /coach/live/stream relays them, with
the latest nudge replayed on connect. Before the fix both used ONE topic, coach:current: rep B's stream
received rep A's call id, title and coaching. Now each user has their own topic (coach/engine.current_topic)
and their own replay slot; A still sees A's.
"""
import json

import pytest
from fastapi.testclient import TestClient

from salescoach import identity, repo
from salescoach.coach.engine import CURRENT_TOPIC, current_topic
from salescoach.live.hub import Hub
from salescoach.plugins import live_coach
from salescoach.web import app as app_module

from test_route_crawl import A, B, ORIGIN, cloud, two_reps  # noqa: F401

pytestmark = pytest.mark.postgres_only

TITLE = "Northwind: CFO balks at 40L price"
TEXT_CALL = [
    ("them", "Good afternoon sir."),
    ("me", "Good afternoon Arjun, thanks for making the time today."),
    ("them", "Sure. So the main problem is detention on our month-end shipments, trucks wait at the plant."),
    ("me", "Here you can see every truck, every lane and the rate card side by side for the quarter."),
    ("them", "Okay. Honestly this looks expensive compared to what we pay the current vendor."),
    ("me", "Fair. What are you comparing it with today?"),
    ("them", "We already have a transporter portal, but our CFO will need to approve anything new."),
]


def _events(body: str) -> list:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def test_a_replay_reaches_only_its_owners_stream(db, two_reps, cloud, monkeypatch):
    with identity.as_user(db, A):
        call_id = repo.create_call(db, source="paste", title=TITLE, wf_state="awaiting_review")
        for i, (channel, text) in enumerate(TEXT_CALL):
            db.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES (?,?,?,?,?)",
                       (call_id, "final", i, channel, text))
        db.commit()
    hub = Hub()
    client = TestClient(app_module.create_app(start_worker=False, live_factory=None, hub=hub), follow_redirects=False)
    assert current_topic(A) != current_topic(B) and CURRENT_TOPIC not in (current_topic(A), current_topic(B))
    b_queue, a_queue, shared = hub.subscribe(current_topic(B)), hub.subscribe(current_topic(A)), hub.subscribe(CURRENT_TOPIC)
    r = client.post(f"/coach/replay/{call_id}", data={"speed": "0"}, headers={"x-test-user": A, "accept": "text/html", **ORIGIN})
    assert r.status_code == 303 and "session=replay-" in r.headers["location"], r.headers
    slot = live_coach.replay_slot(A)
    slot["thread"].join(timeout=60)
    assert live_coach.replay_slot(B)["thread"] is None                        # B's slot is B's own, and empty

    drain = lambda q: [q.get_nowait() for _ in range(q.qsize())]              # noqa: E731
    mine, theirs, nobody = drain(a_queue), drain(b_queue), drain(shared)
    assert theirs == [] and nobody == []                                     # nothing on B's topic or a shared one
    assert TITLE in [m.get("title") for m in mine if m["type"] == "status"]
    assert any(m["type"] == "nudge" and m["call_id"] == call_id for m in mine)
    assert hub.latest(current_topic(B)) == {} and hub.latest(CURRENT_TOPIC) == {}

    # the HTTP streams, opened after the replay: B gets idle and no nudge; A gets A's latest nudge
    monkeypatch.setattr(live_coach, "STREAM_MAX_S", 0.3)
    b_events = _events(client.get("/coach/live/stream", headers={"x-test-user": B}).text)
    assert b_events == [{"type": "status", "state": "idle"}]
    assert call_id not in json.dumps(b_events) and TITLE not in json.dumps(b_events)
    a_events = _events(client.get("/coach/live/stream", headers={"x-test-user": A}).text)
    assert a_events[0]["type"] == "status" and any(e["type"] == "nudge" and e["call_id"] == call_id for e in a_events)
