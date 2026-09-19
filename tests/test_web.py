"""Web UI: every page renders, review actions change the database, the email
goes out exactly once through an injected Gmail, the same-origin guard holds,
evidence audio clips are served, and the live SSE stream relays hub messages.

The workflow worker thread is never started (create_app(start_worker=False));
the pipeline runs synchronously through worker.drain on the scripted fake
model from test_core_pipeline.
"""
import io
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from salescoach import repo
from salescoach.memory import gate
from salescoach.orchestrator import worker
from salescoach.sources import paste
from salescoach.store import stores
from salescoach.web.app import create_app
from test_core_pipeline import CALL1, _script, _setup

ORIGIN = {"origin": "http://127.0.0.1:8140"}
SEND = "Send plant-wise savings breakdown to Arjun"
SIGN = "Prospect to sign pilot agreement"
MEET = "Arjun to set up meeting with CFO and CEO"
MAP = "Map the approval path for the pilot"


class FakeGmail:
    def __init__(self):
        self.sent, self.drafts = [], []

    def send(self, msg):
        self.sent.append(msg)
        return {"message_id": f"m{len(self.sent)}", "thread_id": "t1"}

    def save_draft(self, msg):
        self.drafts.append(msg)
        return {"draft_id": "d1", "message_id": "m-d", "thread_id": "t1"}


class FakeLive:
    """Stands in for live.manager.LiveManager: creates the call row, no callcap, no audio."""

    def __init__(self):
        self.active, self.stopped = None, []

    def start_call(self, title, deal_id=None, lang_mode="auto", participants=()):
        conn = stores.sales()
        try:
            call_id = repo.create_call(conn, source="capture", title=title, deal_id=deal_id, lang_mode=lang_mode,
                                       wf_state="live")
            for p in participants:
                repo.add_participant(conn, call_id, p)
            conn.commit()
        finally:
            conn.close()
        self.active = call_id
        return call_id

    def stop_call(self, call_id=None):
        conn = stores.sales()
        try:
            repo.update_call(conn, call_id, wf_state="captured", ended_at=stores.now())
            conn.commit()
        finally:
            conn.close()
        self.stopped.append(call_id)
        self.active = None
        return {"call_id": call_id}

    def status(self):
        if not self.active:
            return {"active": False}
        return {"active": True, "call_id": self.active, "title": "live", "levels": {}, "alerts": []}


@pytest.fixture
def gmail():
    return FakeGmail()


@pytest.fixture
def live():
    return FakeLive()


@pytest.fixture
def app(db, gmail, live):
    app = create_app(start_worker=False, live_factory=lambda: live)
    app.state.gmail_factory = lambda: gmail
    return app


@pytest.fixture
def client(app):
    return TestClient(app)          # not a context manager: the lifespan, and so the worker, never runs


@pytest.fixture
def processed(db, fake_llm):
    deal, people = _setup(db)
    _script(fake_llm)
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    row = repo.get_call(db, call)
    assert row["wf_state"] == "awaiting_review", row["wf_error"]
    loops = {r["description"]: r["node_id"] for r in db.execute("SELECT * FROM loops WHERE call_id=?", (call,))}
    email = db.execute("SELECT * FROM emails WHERE call_id=? ORDER BY id DESC", (call,)).fetchone()
    return {"deal": deal, "call": call, "people": people, "loops": loops, "email": email}


def _loop(db, loop_id):
    return db.execute("SELECT * FROM loops WHERE node_id=?", (loop_id,)).fetchone()


def _flash(response):
    q = parse_qs(urlparse(response.headers["location"]).query)
    return (q.get("msg") or [""])[0], (q.get("err") or [""])[0]


def test_every_page_renders(client, processed, db):
    call, deal = processed["call"], processed["deal"]
    run_id = db.execute("SELECT id FROM agent_runs WHERE call_id=? AND agent='actions'", (call,)).fetchone()[0]
    pages = {
        "/": ["What needs you", "NWP weekly", "Start recording", "recorded"],
        f"/calls/{call}": ["Interest, no commitment", "Follow-up email", "[SLOTS]", SEND, "Can we lock 6 Oct?",
                           'id="t5"', "garbled", "Complete review", "Needs your decision" if False else "Action review"],
        "/loops": ["Open loops", SEND],
        "/loops?status=all&due=week&owner=me&priority=high": ["Open loops"],
        "/deals": ["NWP pilot"],
        f"/deals/{deal}": ["NWP pilot", "Arjun Kumar", "interest high, urgency unproven", "Who signs the pilot?",
                           "CFO wants plant-wise savings"],
        "/coach": ["How you sell"],
        f"/calls/{call}/runs": ["quality", "invalid", "Rejected or downgraded"],
        f"/runs/{run_id}": ["actions", "Output"],
        "/import": ["Paste a transcript", "Upload a recording"],
        f"/live/{call}": ["not being recorded"],
    }
    for url, needles in pages.items():
        r = client.get(url)
        assert r.status_code == 200, url
        for needle in needles:
            assert needle in r.text, (url, needle)
    assert client.get("/calls/call-nope").status_code == 404
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_loop_review_actions_change_the_db(client, processed, db):
    call, loops = processed["call"], processed["loops"]
    r = client.post(f"/loops/{loops[SEND]}/confirm", data={"next": f"/calls/{call}"}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/calls/{call}")
    assert _loop(db, loops[SEND])["review_state"] == "confirmed"

    client.post(f"/loops/{loops[SIGN]}/reject", headers=ORIGIN, follow_redirects=False)
    rejected = _loop(db, loops[SIGN])
    assert (rejected["review_state"], rejected["status"]) == ("rejected", "cancelled")

    r = client.post(f"/loops/{loops[MEET]}/edit", headers=ORIGIN, follow_redirects=False, data={
        "description": "Arjun to book the CFO and CEO meeting", "owner": "prospect", "owner_name": "Arjun Kumar",
        "due_date": "2026-10-06", "priority": "critical", "status": "waiting"})
    assert r.status_code == 303
    edited = _loop(db, loops[MEET])
    assert edited["description"] == "Arjun to book the CFO and CEO meeting"
    assert (edited["due_date"], edited["due_date_confidence"]) == ("2026-10-06", "explicit")
    assert (edited["priority"], edited["status"], edited["review_state"]) == ("critical", "waiting", "confirmed")

    bad = client.post(f"/loops/{loops[MEET]}/edit", headers=ORIGIN, follow_redirects=False, data={
        "description": "x", "owner": "nobody", "due_date": "soon", "priority": "high", "status": "open"})
    assert "owner must be" in _flash(bad)[1] and _loop(db, loops[MEET])["owner"] == "prospect"

    client.post(f"/loops/{loops[MAP]}/status", data={"status": "done", "next": "/loops"}, headers=ORIGIN,
                follow_redirects=False)
    done = _loop(db, loops[MAP])
    assert done["status"] == "done" and done["closed_at"]

    r = client.post(f"/calls/{call}/loops", headers=ORIGIN, follow_redirects=False, data={
        "description": "Share the Pant Nagar pilot plan", "owner": "me", "type": "my_action",
        "due_date": "2026-09-15", "priority": "high"})
    added = db.execute("SELECT * FROM loops WHERE description='Share the Pant Nagar pilot plan'").fetchone()
    assert added["source"] == "user_input" and added["review_state"] == "confirmed"

    open_page = client.get("/loops").text
    assert "Arjun to book the CFO and CEO meeting" in open_page and MAP not in open_page
    assert MAP in client.get("/loops?status=done").text
    assert SIGN in client.get("/loops?status=all").text


def test_parked_proposal_accept(client, processed, db):
    loop_id = processed["loops"][SEND]
    gate.park(db, gate.Proposed(loop_id, "loops", "status", "waiting", "medium",
                                {"kind": "call", "ref": processed["call"], "turns": [3], "reason": "maybe later"}))
    db.commit()
    page = client.get(f"/calls/{processed['call']}").text
    assert "Needs your decision" in page and "maybe later" in page
    conflict = db.execute("SELECT id FROM memory_conflicts WHERE entity_id=? AND status='open'", (loop_id,)).fetchone()
    client.post(f"/conflicts/{conflict['id']}/resolve", data={"accept": "1"}, headers=ORIGIN, follow_redirects=False)
    assert _loop(db, loop_id)["status"] == "waiting"
    again = client.post(f"/conflicts/{conflict['id']}/resolve", data={"accept": "1"}, headers=ORIGIN,
                        follow_redirects=False)
    assert "already settled" in _flash(again)[1]


def test_email_edit_then_send_exactly_once(client, processed, db, gmail):
    call, email = processed["call"], processed["email"]
    eid = email["id"]

    # The draft still carries [SLOTS]: Send is refused and nothing reaches Gmail.
    r = client.post(f"/emails/{eid}/send", headers=ORIGIN, follow_redirects=False)
    assert "refused" in _flash(r)[1] and "calendar" in _flash(r)[1]
    assert gmail.sent == []

    body = email["body"].replace("CFO meeting on [SLOTS]", "CFO meeting on Tue 6 Oct, 11am")
    form = {"to": "arjun@northwind.test", "cc": "", "subject": email["subject"], "body": body.replace("\n", "\r\n")}
    r = client.post(f"/emails/{eid}/save", data=form, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and _flash(r)[0] == "Draft saved."
    saved = db.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    assert saved["body"] == body and saved["status"] == "drafted"
    assert "blocks send" not in client.get(f"/calls/{call}").text

    r = client.post(f"/emails/{eid}/send", data=form, headers=ORIGIN, follow_redirects=False)
    assert _flash(r)[0] == "Sent."
    assert len(gmail.sent) == 1 and gmail.sent[0].to == ["arjun@northwind.test"]
    assert "Tue 6 Oct, 11am" in gmail.sent[0].body and "\r" not in gmail.sent[0].body
    assert db.execute("SELECT status FROM emails WHERE id=?", (eid,)).fetchone()[0] == "sent"
    assert repo.get_call(db, call)["wf_state"] == "done"

    again = client.post(f"/emails/{eid}/send", data=form, headers=ORIGIN, follow_redirects=False)
    assert "Nothing was sent again" in _flash(again)[0]
    assert len(gmail.sent) == 1
    assert "Sent " in client.get(f"/calls/{call}").text


def test_save_to_drafts_keeps_the_call_open(client, processed, db, gmail):
    call, email = processed["call"], processed["email"]
    body = email["body"].replace("[SLOTS]", "Tue 6 Oct, 11am")
    form = {"to": "arjun@northwind.test", "cc": "", "subject": email["subject"], "body": body}
    r = client.post(f"/emails/{email['id']}/draft", data=form, headers=ORIGIN, follow_redirects=False)
    assert "Gmail Drafts" in _flash(r)[0]
    assert len(gmail.drafts) == 1 and gmail.sent == []
    assert repo.get_call(db, call)["wf_state"] == "awaiting_review"
    client.post(f"/emails/{email['id']}/mark-sent", headers=ORIGIN, follow_redirects=False)
    assert repo.get_call(db, call)["wf_state"] == "done" and gmail.sent == []


def test_skip_email_closes_the_call(client, processed, db, gmail):
    call, email = processed["call"], processed["email"]
    client.post(f"/emails/{email['id']}/skip", headers=ORIGIN, follow_redirects=False)
    assert db.execute("SELECT status FROM emails WHERE id=?", (email["id"],)).fetchone()[0] == "rejected"
    assert repo.get_call(db, call)["wf_state"] == "done" and gmail.sent == []


def test_cross_origin_post_refused(client, processed, db, gmail):
    loop_id = processed["loops"][SEND]
    for headers in ({"origin": "http://evil.test"}, {"referer": "https://evil.test/page"}, {},
                    {"origin": "null"}, {"origin": "http://127.0.0.1.evil.test"},
                    {"origin": "http://evil.test", "referer": "http://127.0.0.1:8140/"}):
        r = client.post(f"/loops/{loop_id}/confirm", headers=headers, follow_redirects=False)
        assert r.status_code == 403, headers
    r = client.post(f"/emails/{processed['email']['id']}/send", headers={"origin": "http://evil.test"},
                    follow_redirects=False)
    assert r.status_code == 403 and gmail.sent == []
    assert _loop(db, loop_id)["review_state"] == "proposed"
    ok = client.post(f"/loops/{loop_id}/confirm", headers={"referer": "http://localhost:8140/loops"},
                     follow_redirects=False)
    assert ok.status_code == 303 and _loop(db, loop_id)["review_state"] == "confirmed"


def _audio_call(db, tmp_path, with_audio=True):
    call = repo.create_call(db, source="audio_file", title="Recorded call", wf_state="awaiting_review")
    if with_audio:
        audio_dir = tmp_path / "calls" / call
        audio_dir.mkdir(parents=True)
        sr = 16000
        t = np.arange(sr * 4) / sr
        for name, freq in (("me", 220), ("them", 330)):
            sf.write(str(audio_dir / f"{name}.flac"), (0.3 * np.sin(2 * np.pi * freq * t)).astype("float32"), sr,
                     format="FLAC", subtype="PCM_16")
        repo.update_call(db, call, audio_dir=str(audio_dir))
    for idx, (channel, start, end) in enumerate([("me", 0.5, 1.5), ("them", 1.6, 3.0), ("them", None, None)]):
        db.execute("INSERT INTO turns(call_id,tier,idx,channel,t_start,t_end,text) VALUES (?,?,?,?,?,?,?)",
                   (call, "final", idx, channel, start, end, f"turn {idx}"))
    db.commit()
    return call


def test_clip_returns_wav_of_the_cited_turns(client, db, tmp_path):
    call = _audio_call(db, tmp_path)
    r = client.get(f"/calls/{call}/clip?turns=0,1")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    data, sr = sf.read(io.BytesIO(r.content))
    assert sr == 16000 and data.ndim == 1
    assert abs(len(data) / sr - 3.3) < 0.01            # 0.1 s .. 3.4 s: the span plus padding
    assert client.get(f"/calls/{call}/clip?turns=2").status_code == 404      # no timestamps
    no_audio = _audio_call(db, tmp_path, with_audio=False)
    assert client.get(f"/calls/{no_audio}/clip?turns=0").status_code == 404
    assert 'class="play"' in client.get(f"/calls/{call}").text
    assert 'class="play"' not in client.get(f"/calls/{no_audio}").text


def test_sse_streams_hub_messages(app, client):
    hub_module = pytest.importorskip("salescoach.live.hub", reason="live hub module (salescoach/live/hub.py) not built yet")
    hub = app.state.hub
    assert hub is hub_module.hub
    app.state.sse_keepalive_s = 0.05
    call_id = "call-test-sse"
    topic = f"call:{call_id}"

    def publisher():
        deadline = time.monotonic() + 5
        while hub.subscribers(topic) == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)                     # long enough for at least one keepalive
        hub.publish(topic, {"type": "segment", "idx": 0, "channel": "them", "t_start": 1.0, "t_end": 2.0,
                            "text": "Namaste, shall we begin?"})
        hub.publish(topic, {"type": "ended", "call_id": call_id})

    thread = threading.Thread(target=publisher, daemon=True)
    thread.start()
    with client.stream("GET", f"/live/{call_id}/events") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    thread.join(timeout=5)
    events = [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["segment", "ended"]
    assert events[0]["text"] == "Namaste, shall we begin?" and events[0]["channel"] == "them"
    assert ": keepalive" in body
    assert hub.subscribers(topic) == 0      # unsubscribed when the stream closed


def test_start_and_stop_a_live_call(client, db, live):
    deal, (arjun, me) = _setup(db)
    r = client.post("/live/start", headers=ORIGIN, follow_redirects=False, data={
        "title": "NWP live", "deal_id": deal, "lang_mode": "hinglish", "participants": [arjun]})
    assert r.status_code == 303
    call_id = urlparse(r.headers["location"]).path.rsplit("/", 1)[1]
    assert live.active == call_id
    assert {p["node_id"] for p in repo.call_participants(db, call_id)} == {arjun, me}
    page = client.get(f"/live/{call_id}").text
    assert "Stop call" in page and f'data-events="/live/{call_id}/events"' in page and "NWP pilot" in page
    assert "Live" in client.get("/").text
    r = client.post(f"/live/{call_id}/stop", headers=ORIGIN, follow_redirects=False)
    assert urlparse(r.headers["location"]).path == f"/calls/{call_id}" and live.stopped == [call_id]


def test_retry_and_redraft_queue_process_call(client, processed, db):
    call = processed["call"]
    client.post(f"/calls/{call}/redraft", headers=ORIGIN, follow_redirects=False)
    ev = db.execute("SELECT * FROM wf_events WHERE type='PROCESS_CALL' AND entity_id=? ORDER BY id DESC",
                    (call,)).fetchone()
    assert ev["status"] == "pending" and json.loads(ev["payload"]) == {"from": "email_drafted", "force": True}
    assert "Re-running" in client.get(f"/calls/{call}").text

    repo.update_call(db, call, wf_error="summarized: AgentFailed: summary failed: boom")
    db.commit()
    assert "Retry from Summary" in client.get("/").text
    client.post(f"/calls/{call}/retry", data={"from_step": "summarized"}, headers=ORIGIN, follow_redirects=False)
    ev = db.execute("SELECT * FROM wf_events WHERE type='PROCESS_CALL' AND entity_id=? ORDER BY id DESC",
                    (call,)).fetchone()
    assert json.loads(ev["payload"]) == {"from": "summarized", "force": False}
    assert repo.get_call(db, call)["wf_error"] is None


def test_import_text_creates_a_call(client, db):
    deal, (arjun, me) = _setup(db)
    r = client.post("/import/text", headers=ORIGIN, follow_redirects=False, data={
        "title": "Pasted call", "deal_id": deal, "started_at": "2026-09-10T15:30", "lang_mode": "en",
        "participants": [arjun], "text": "Me: Hello Arjun.\r\nThem: Hi Maya."})
    call_id = urlparse(r.headers["location"]).path.rsplit("/", 1)[1]
    row = repo.get_call(db, call_id)
    assert row["started_at"] == "2026-09-10T15:30:00+05:30" and row["deal_id"] == deal
    assert [t["text"] for t in repo.turns(db, call_id)] == ["Hello Arjun.", "Hi Maya."]
    assert db.execute("SELECT status FROM wf_events WHERE type='CALL_ENDED' AND entity_id=?",
                      (call_id,)).fetchone()[0] == "pending"
    assert "worker is not running" in client.get(f"/calls/{call_id}").text
    empty = client.post("/import/text", data={"title": "x", "text": "no speakers"}, headers=ORIGIN,
                        follow_redirects=False)
    assert "no speaker turns" in _flash(empty)[1]


def test_deal_create_and_link_a_call(client, db):
    r = client.post("/deals", headers=ORIGIN, follow_redirects=False,
                    data={"name": "Harborline pilot", "account": "Harborline", "domains": "harborline.test", "stage": "discovery"})
    deal_id = urlparse(r.headers["location"]).path.rsplit("/", 1)[1]
    client.post(f"/deals/{deal_id}/people", headers=ORIGIN, follow_redirects=False,
                data={"name": "Asha Rao", "email": "asha@harborline.test", "title": "CFO", "role": "economic buyer"})
    stake = {p["name"]: p for p in repo.deal_people(db, deal_id)}
    assert stake["Asha Rao"]["role_in_deal"] == "economic buyer"
    assert stake["Asha Rao"]["account_id"] == db.execute("SELECT account_id FROM deals WHERE node_id=?",
                                                         (deal_id,)).fetchone()[0]
    assert "Asha Rao" in client.get(f"/deals/{deal_id}").text

    call = paste.import_text(db, "Me: Hello.\nRavi: Namaste.\nAsha: Hi.", "Unlinked call")
    page = client.get(f"/calls/{call}").text
    assert "Not linked to a deal" in page and "Who was speaking" in page
    client.post(f"/calls/{call}/deal", data={"deal_id": deal_id}, headers=ORIGIN, follow_redirects=False)
    assert repo.get_call(db, call)["deal_id"] == deal_id
    client.post(f"/calls/{call}/participants", data={"name": "Ravi Iyer", "email": "ravi@harborline.test"},
                headers=ORIGIN, follow_redirects=False)
    ravi = repo.find_person_by_email(db, "ravi@harborline.test")
    assert ravi in {p["node_id"] for p in repo.call_participants(db, call)}
    assert ravi in {p["node_id"] for p in repo.deal_people(db, deal_id)}

    client.post(f"/calls/{call}/speakers", data={"cluster": ["Ravi", "Asha"], "person": [ravi, stake["Asha Rao"]["node_id"]]},
                headers=ORIGIN, follow_redirects=False)
    mapped = {t["speaker_cluster"]: t["person_id"] for t in repo.turns(db, call) if t["channel"] == "them"}
    assert mapped == {"Ravi": ravi, "Asha": stake["Asha Rao"]["node_id"]}
    assert "Ravi Iyer" in client.get(f"/calls/{call}").text


def test_complete_review_publishes_event(client, processed, db):
    call = processed["call"]
    client.post(f"/calls/{call}/complete", headers=ORIGIN, follow_redirects=False)
    assert repo.get_call(db, call)["wf_state"] == "reviewed"
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='REVIEW_COMPLETED' AND entity_id=?",
                      (call,)).fetchone()[0] == 1
    assert "Review completed" in client.get(f"/calls/{call}").text


def test_default_app_and_worker_lifespan(db):
    plain = TestClient(create_app(start_worker=False))       # real live module if present, guarded if not
    assert plain.get("/").status_code == 200
    with TestClient(create_app(start_worker=True, live_factory=None)) as c:
        thread = c.app.state.worker_thread
        assert c.app.state.worker is not None and thread.is_alive()
        assert "worker off" not in c.get("/").text
    thread.join(timeout=5)
    assert not thread.is_alive() and c.app.state.worker is None
    assert "worker off" in plain.get("/").text
