"""Phase 4 scheduler and web: the duties start and stop with the server and
record their last run; follow-ups run daily at 09:30 IST with a catch-up; the
pages and Today fragments render; fill-slots, nudge Send and "It was not sent"
work through the same-origin guard; the CLI subcommands answer offline."""
import json
import threading
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from salescoach.automation import calendar, connector, followup, replies, scheduler
from salescoach.orchestrator import worker
from salescoach.plugins import execution
from salescoach.sources import paste
from salescoach.store import stores
from salescoach.store.stores import get_state, get_user_state, now
from salescoach.web.app import create_app
from test_p4_followup import run_cli
from test_p4_support import (ARJUN, ORIGIN, TODAY, FakeCalendar, FakeGmail, at, clock, decision, events_of,  # noqa: F401
                             ev, gmail_msg, link_nudge, loop_row, make_email, make_loop, nudge, world)

SLOTS_BODY = "Hi Arjun,\n\nHappy to walk Anita through the plant-wise numbers.\n[SLOTS]\n\nThanks,\nMaya"
UNKNOWN = "delivery unknown: TimeoutError: read timed out. Check Gmail Sent before doing anything."
FILLED = "I'm free Wed 16 Sep at 11:00, Thu 17 Sep at 15:30 or Tue 22 Sep at 11:00 IST. Would one of those work?"


def _wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _state(key):
    conn = stores.sales()
    try:
        return get_user_state(conn, key)                   # duties are per user: their bookkeeping is too
    finally:
        conn.close()


# ---- scheduler --------------------------------------------------------------------------------

def test_duties_run_record_their_state_and_stop(db):
    stop, calls = threading.Event(), []

    def gone(conn):
        raise scheduler.Unavailable("no Gmail credentials", retry_s=0.05)

    duties = [scheduler.Duty("probe", lambda conn: calls.append(1) or {"runs": len(calls)}, lambda: 0.05, 0.01),
              scheduler.Duty("gone", gone, lambda: 0.05, 0.01),
              scheduler.Duty("broken", lambda conn: 1 / 0, lambda: 0.05, 0.01)]
    threads = scheduler.start(stores.db_path(), stop, duties)
    assert _wait_for(lambda: len(calls) >= 2 and _state("automation:gone:unavailable")
                     and _state("automation:broken:last_error"))
    stop.set()
    for t in threads:
        t.join(2)
        assert not t.is_alive()
    assert json.loads(_state("automation:probe:last_result"))["runs"] >= 1 and _state("automation:probe:last_run")
    assert json.loads(_state("automation:gone:unavailable"))["why"] == "no Gmail credentials"
    assert "ZeroDivisionError" in json.loads(_state("automation:broken:last_error"))["error"]


def test_the_server_duties_start_and_stop_without_touching_gmail_or_claude(db, monkeypatch):
    monkeypatch.setattr(scheduler, "_gmail", lambda: (_ for _ in ()).throw(AssertionError("Gmail touched")))
    monkeypatch.setattr(connector, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude touched")))
    stop = threading.Event()
    threads = scheduler.start(stores.db_path(), stop)
    assert sorted(t.name for t in threads) == \
        ["salescoach-autosend", "salescoach-calendar", "salescoach-followups", "salescoach-recorder",
             "salescoach-replies", "salescoach-retention"]
    assert all(t.daemon and t.is_alive() for t in threads)
    stop.set()
    for t in threads:
        t.join(2)
        assert not t.is_alive()

    stop2 = threading.Event()
    before = set(threading.enumerate())
    execution.start_background(stores.db_path(), stop2)             # the plugin seam `serve` uses
    started = [t for t in threading.enumerate() if t not in before and t.name.startswith("salescoach-")]
    assert len(started) == 6
    stop2.set()
    assert _wait_for(lambda: not any(t.is_alive() for t in started), 3)


def test_followups_run_daily_at_0930_with_a_catch_up(db, world, clock, fake_llm):
    make_loop(db, world.deal, "Send the plant-wise breakdown", owner="me", owner_name=None, type_="my_action")
    clock.set(at(TODAY, "08:00"))
    assert scheduler.run_followups(db) == {"skipped": "waiting for 09:30 IST"}
    clock.set(at(TODAY, "14:00"))                                   # started late: catches up once
    assert scheduler.run_followups(db) == {"decisions": 1}
    assert get_user_state(db, "automation:followups:ran_for") == TODAY.isoformat()
    assert scheduler.run_followups(db) == {"skipped": "already ran today"}
    clock.set(at(TODAY + timedelta(days=1), "09:31"))
    assert "decisions" in scheduler.run_followups(db)
    assert scheduler.seconds_until("09:30", at(TODAY, "09:00")) == 1800
    assert scheduler.seconds_until("09:30", at(TODAY, "10:00")) == 23.5 * 3600


def test_replies_duty_waits_for_gmail_credentials(db, monkeypatch):
    monkeypatch.setattr(scheduler, "_gmail", lambda: (_ for _ in ()).throw(FileNotFoundError("no token file")))
    duty = scheduler._replies_duty()
    with pytest.raises(scheduler.Unavailable) as exc:
        duty.run(db)
    assert exc.value.retry_s == 3600 and duty.interval_s() == 15 * 60
    gmail = FakeGmail()
    monkeypatch.setattr(scheduler, "_gmail", lambda: gmail)
    assert scheduler._replies_duty().run(db) == {"threads": 0, "new": [], "errors": []}


def test_calendar_duty_backs_off_and_scans(db, world, clock, monkeypatch):
    monkeypatch.setattr(connector, "discover",
                        lambda conn=None, refresh=False: (_ for _ in ()).throw(connector.ConnectorError("not loaded")))
    with pytest.raises(scheduler.Unavailable) as exc:
        scheduler.run_calendar(db)
    assert exc.value.retry_s == 6 * 3600

    monkeypatch.setattr(connector, "discover", lambda conn=None, refresh=False: {"prefix": "p__", "tools": []})
    meeting = ev(at(date(2026, 9, 16), "11:00"), id="nwp", title="NWP weekly",
                 attendees=[{"email": ARJUN, "self": False, "response": "accepted"}])
    monkeypatch.setattr(calendar, "ConnectorCalendar", lambda conn=None: FakeCalendar([meeting]))
    assert scheduler.run_calendar(db) == {"meetings": 1, "deal_meetings": 1, "new": 1}
    assert scheduler.run_calendar(db) == {"meetings": 1, "deal_meetings": 1, "new": 0}


def test_autosend_duty_is_a_no_op_as_shipped(db):
    assert scheduler.run_autosend(db) == {"skipped": "auto_send.enabled is false"}


# ---- web ----------------------------------------------------------------------------------------

@pytest.fixture
def gmail():
    return FakeGmail()


@pytest.fixture
def app(db, gmail, clock):
    app = create_app(start_worker=False, live_factory=None)
    app.state.gmail_factory = lambda: gmail
    app.state.calendar_factory = lambda conn: FakeCalendar()
    return app


@pytest.fixture
def client(app):
    return TestClient(app)          # not a context manager: no lifespan, no worker, no scheduler


def _flash(response):
    q = parse_qs(urlparse(response.headers["location"]).query)
    return (q.get("msg") or [""])[0], (q.get("err") or [""])[0]


def _post(client, url, **data):
    return client.post(url, headers=ORIGIN, follow_redirects=False, data=data or None)


def test_pages_and_today_sections_render(client, db, world, fake_llm):
    make_loop(db, world.deal)
    fake_llm.responses.update({"FollowupDecision": decision(), "NudgeDraft": nudge()})
    [r] = followup.evaluate_due(db, TODAY)
    make_loop(db, world.deal, "Anita to confirm the pilot budget owner", owner_name="Anita Rao")   # due, undecided
    make_email(db, world.deal, status="sent", sent_at=now(), thread_id="t-1", subject="NWP next steps")
    replies.poll(db, FakeGmail(threads={"t-1": [gmail_msg("m1", "The CFO meeting is done.")]}))
    fake_llm.responses["ReplyAnalysis"] = {"summary": "Arjun says the CFO meeting happened.", "items": [],
                                           "ignored_instructions": [], "needs_user": False}
    start = at(date(2026, 9, 16), "11:00")
    db.execute("INSERT INTO calendar_meetings(event_id,deal_id,title,start_at,end_at,attendees,first_seen_at,"
               "prep_status,prep_ref) VALUES ('ev1',?,?,?,?,?,?,'ready','42')",
               (world.deal, "NWP weekly", start.isoformat(), (start + timedelta(minutes=30)).isoformat(),
                json.dumps([ARJUN]), now()))
    db.commit()
    worker.drain(db)

    pages = {
        "/followups": ["Nudges to send", "The CFO meeting", "Anita to confirm the pilot budget owner",
                       "Draft a nudge now", "Check due loops now", r["rationale"]],
        f"/nudges/{r['email_id']}": ["Why this nudge", "Arjun to set up the meeting with the CFO", "Send ↗",
                                     "Auto-send would not send this", "drafted this"],
        "/replies": ["Arjun says the CFO meeting happened.", "Mark reviewed", "Check Gmail now"],
        "/automation/today": ["Follow-ups due", "Replies received", "Upcoming calls", "NWP weekly",
                              f'href="/deals/{world.deal}/prep"', "Prep brief"],
        "/": ["Follow-ups due", "Replies received", "Upcoming calls", "/static/automation.css",
              'href="/followups"', f'href="/nudges/{r["email_id"]}"'],
    }
    for url, needles in pages.items():
        page = client.get(url)
        assert page.status_code == 200, url
        for needle in needles:
            assert needle in page.text, (url, needle)
    assert ("/followups", "Follow-ups") in execution.NAV
    assert client.get("/static/automation.css").status_code == 200
    assert client.get("/nudges/99999").status_code == 404


def test_fill_times_from_calendar_on_a_nudge(client, app, db, world):
    eid = make_email(db, world.deal, body=SLOTS_BODY)
    page = client.get(f"/nudges/{eid}").text
    assert "Fill times from calendar" in page and f'formaction="/emails/{eid}/fill-slots"' in page
    r = _post(client, f"/emails/{eid}/fill-slots", to=ARJUN, cc="", subject="The CFO meeting", body=SLOTS_BODY)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/nudges/{eid}")
    msg, err = _flash(r)
    assert msg == f"Calendar-verified times filled in: {FILLED}" and not err
    assert FILLED in db.execute("SELECT body FROM emails WHERE id=?", (eid,)).fetchone()[0]
    page = client.get(f"/nudges/{eid}").text
    assert "Times calendar-verified" in page and "Fill times from calendar" not in page

    other = make_email(db, world.deal, body=SLOTS_BODY)
    app.state.calendar_factory = lambda conn: FakeCalendar(error="connector not authorised")
    msg, err = _flash(_post(client, f"/emails/{other}/fill-slots"))
    assert not msg and err == "Times not filled: calendar unreachable, so [SLOTS] stays: connector not authorised"
    assert db.execute("SELECT body FROM emails WHERE id=?", (other,)).fetchone()[0] == SLOTS_BODY
    assert "Last calendar fill" in client.get(f"/nudges/{other}").text


def test_call_page_email_buttons(client, db, fake_llm):
    from test_core_pipeline import CALL1, _script, _setup
    deal, people = _setup(db)
    _script(fake_llm)
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    email = db.execute("SELECT * FROM emails WHERE call_id=? ORDER BY id DESC", (call,)).fetchone()
    page = client.get(f"/calls/{call}").text
    assert "[SLOTS]" in page and "Fill times from calendar" in page
    assert f'formaction="/emails/{email["id"]}/fill-slots"' in page and "It was not sent" not in page

    r = _post(client, f"/emails/{email['id']}/fill-slots")
    assert r.headers["location"].startswith(f"/calls/{call}")
    body = db.execute("SELECT body FROM emails WHERE id=?", (email["id"],)).fetchone()[0]
    assert "[SLOTS]" not in body and "Wed 16 Sep at 11:00" in body
    assert "Meeting times calendar-verified" in client.get(f"/calls/{call}").text

    db.execute("UPDATE emails SET status='sending', error=? WHERE id=?", (UNKNOWN, email["id"]))
    db.commit()
    page = client.get(f"/calls/{call}").text
    assert "It was not sent" in page and f'action="/emails/{email["id"]}/not-sent"' in page
    msg, _ = _flash(_post(client, f"/emails/{email['id']}/not-sent"))
    assert msg.startswith("Marked as not sent")
    assert db.execute("SELECT status FROM emails WHERE id=?", (email["id"],)).fetchone()[0] == "failed"

    db.execute("UPDATE emails SET status='sending', error=NULL WHERE id=?", (email["id"],))
    db.commit()
    assert "It was not sent" not in client.get(f"/calls/{call}").text  # only after an unknown-delivery error


def test_nudge_send_goes_out_once_and_counts(client, db, world, gmail):
    loop = make_loop(db, world.deal)
    eid = make_email(db, world.deal)
    link_nudge(db, loop, eid, world.deal)
    msg, err = _flash(_post(client, f"/nudges/{eid}/send"))
    assert msg == "Sent." and not err and len(gmail.sent) == 1
    assert db.execute("SELECT approved_by FROM emails WHERE id=?", (eid,)).fetchone()[0] == "user:local"   # user:<actor id>
    worker.drain(db)
    assert loop_row(db, loop)["follow_up_count"] == 1
    msg, _ = _flash(_post(client, f"/nudges/{eid}/send"))
    assert msg == "Already done. Nothing was sent again." and len(gmail.sent) == 1


def test_a_nudge_stuck_in_sending_can_be_released(client, db, world):
    eid = make_email(db, world.deal, status="sending", error=UNKNOWN)
    page = client.get(f"/nudges/{eid}").text
    assert "It was not sent" in page and f'action="/nudges/{eid}/not-sent"' in page
    msg, _ = _flash(_post(client, f"/nudges/{eid}/not-sent"))
    assert msg == "Marked as not sent. You can send it again."
    assert db.execute("SELECT status FROM emails WHERE id=?", (eid,)).fetchone()[0] == "failed"
    _, err = _flash(_post(client, f"/nudges/{eid}/not-sent"))
    assert err == "Only an email stuck in sending can be released."
    quiet = make_email(db, world.deal, status="sending")
    assert "It was not sent" not in client.get(f"/nudges/{quiet}").text


def test_followup_routes_queue_work_for_the_worker(client, db, world, fake_llm):
    own = make_loop(db, world.deal, "Send the plant-wise breakdown", owner="me", owner_name=None, type_="my_action")
    buyer = make_loop(db, world.deal, next_check_at="2026-09-30")
    assert _post(client, "/followups/run").status_code == 303
    assert len(events_of(db, "FOLLOW_UP_RUN")) == 1
    assert db.execute("SELECT COUNT(*) FROM followup_decisions").fetchone()[0] == 0    # the worker decides
    worker.drain(db)
    d = db.execute("SELECT * FROM followup_decisions WHERE loop_id=?", (own,)).fetchone()
    assert (d["check_name"], d["decision"]) == ("own_commitment", "ask_user")

    fake_llm.responses["NudgeDraft"] = nudge()
    _post(client, f"/followups/{buyer}/nudge", next="/followups")
    worker.drain(db)
    assert db.execute("SELECT status FROM emails WHERE kind='nudge'").fetchone()[0] == "drafted"

    msg, _ = _flash(_post(client, f"/followups/{own}/snooze", until="2026-09-25"))
    assert msg.startswith("Snoozed until") and loop_row(db, own)["next_check_at"] == "2026-09-25"
    assert _flash(_post(client, f"/followups/{own}/snooze", until="soon"))[1] == "Pick a date to look again."
    _post(client, f"/followups/{own}/still-open")
    assert db.execute("SELECT check_name FROM followup_decisions ORDER BY id DESC LIMIT 1").fetchone()[0] == "still_open"
    assert _post(client, "/followups/loop-nope/snooze", until="2026-09-25").status_code == 404

    refused = client.post("/followups/run", headers={"origin": "http://evil.test"}, follow_redirects=False)
    assert refused.status_code == 403 and len(events_of(db, "FOLLOW_UP_RUN")) == 1


def test_reply_routes(client, db, world, gmail):
    make_email(db, world.deal, status="sent", sent_at=now(), thread_id="t-1")
    gmail.threads["t-1"] = [gmail_msg("m1", "Noted, will revert.")]
    msg, _ = _flash(_post(client, "/replies/poll"))
    assert msg == "Checked 1 thread(s): 1 new reply."
    rid = db.execute("SELECT id FROM email_replies").fetchone()[0]
    assert "Noted, will revert." in client.get("/replies").text
    _post(client, f"/replies/{rid}/reviewed")
    assert db.execute("SELECT status FROM email_replies").fetchone()[0] == "reviewed"
    assert "Noted, will revert." not in client.get("/replies").text
    assert "Noted, will revert." in client.get("/replies?all=1").text
    _post(client, f"/replies/{rid}/reanalyze")
    assert events_of(db, "STAKEHOLDER_REPLY_RECEIVED")[-1]["payload"] == {"reply_id": rid, "force": True}


# ---- CLI ---------------------------------------------------------------------------------------

def test_cli_calendar_and_replies_answer_offline(db, world, clock, monkeypatch, capsys):
    monkeypatch.setattr(calendar, "ConnectorCalendar", lambda conn=None: FakeCalendar())
    run_cli("calendar", "slots", "--days", "5")
    out = capsys.readouterr().out
    assert "slot Wed 16 Sep at 11:00 IST" in out and FILLED in out

    tools = ["mcp__claude_ai_Google_Calendar__list_events", "mcp__claude_ai_Google_Calendar__list_calendars"]
    monkeypatch.setattr(connector, "_run", lambda *a: json.dumps({"type": "system", "subtype": "init", "tools": tools}))
    run_cli("calendar", "tools", "--refresh")
    assert '"prefix": "mcp__claude_ai_Google_Calendar__"' in capsys.readouterr().out

    run_cli("calendar", "upcoming")
    assert capsys.readouterr().out.strip().startswith("[]")

    gmail = FakeGmail()
    monkeypatch.setattr(scheduler, "_gmail", lambda: gmail)
    run_cli("replies", "poll")
    assert '"threads": 0' in capsys.readouterr().out
