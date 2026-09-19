"""Calendar (all meetings) and recording choice: every non-cancelled meeting is stored and shown,
Maya arms the ones to record, the recorder starts them on time (never over a live call), stops
them after the grace period, and "Record now" starts at once with the meeting's people attached."""
import json
from datetime import timedelta

import pytest

from salescoach import repo
from salescoach.automation import calendar, scheduler
from salescoach.orchestrator import bus, worker
from salescoach.web.app import create_app
from test_p4_support import ARJUN, TUESDAY, FakeCalendar, clock, ev, events_of, world  # noqa: F401

ME = {"email": "maya@tessel.test", "self": True, "response": "accepted"}


def _guest(email, response="accepted"):
    return {"email": email, "self": False, "response": response}


def _week():
    t = TUESDAY
    return [
        ev(t + timedelta(hours=3), id="nwp", title="NWP weekly", attendees=[ME, _guest(ARJUN)],
           meeting_url="https://meet.google.com/abc-defg-hij"),
        ev(t + timedelta(days=1), id="adv", title="Advisors", attendees=[ME, _guest("x@bcg.test")]),
        ev(t + timedelta(days=1, hours=2), id="int", title="Internal sync", attendees=[ME, _guest("piyush@tessel.test")]),
        ev(t + timedelta(days=2), id="dec", title="Declined", attendees=[ME, _guest(ARJUN)], declined_by_me=True),
        ev(t + timedelta(days=2, hours=1), id="can", title="Cancelled", attendees=[ME, _guest(ARJUN)], status="cancelled"),
        ev(t + timedelta(days=3), id="ooo", title="Leave", attendees=[], all_day=True),
        ev(t + timedelta(days=4), id="ooo2", title="Out of Office — Maya", attendees=[]),
        ev(t + timedelta(days=4, hours=2), id="free", title="Focus block", attendees=[], transparency="transparent"),
    ]


class FakeManager:
    """The live manager's surface the recorder uses; records calls instead of capturing."""

    def __init__(self, fail=None):
        self.started, self.stopped, self.fail, self.active = [], [], fail, None

    def start_call(self, title, deal_id=None, lang_mode="auto", participants=()):
        if self.fail:
            raise self.fail
        if self.active:
            raise RuntimeError("LiveCallActive")
        self.active = f"call-{len(self.started) + 1}"
        self.started.append({"call_id": self.active, "title": title, "deal_id": deal_id, "participants": list(participants)})
        return self.active

    def stop_call(self, call_id=None):
        self.stopped.append(call_id or self.active)
        self.active = None
        return {}

    def status(self):
        return {"active": True, "call_id": self.active} if self.active else {"active": False}


def test_every_real_meeting_is_stored_and_only_deal_ones_are_prepped(db, world, clock):
    found = calendar.sync_events(db, calendar=FakeCalendar(_week()))
    assert sorted(m["event_id"] for m in found) == ["adv", "int", "nwp"]        # declined, cancelled, all-day, OOO, free skipped
    assert {m["event_id"]: m["deal_id"] for m in found} == {"nwp": world.deal, "adv": None, "int": None}
    assert next(m for m in found if m["event_id"] == "nwp")["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert [e["payload"]["event_id"] for e in events_of(db, "NEXT_CALL_SCHEDULED")] == ["nwp"]
    shown = calendar.upcoming_meetings(db)
    assert [m["event_id"] for m in shown] == ["nwp", "adv", "int"] and not any(m["armed"] for m in shown)
    assert calendar.upcoming_deal_meetings(db, calendar=FakeCalendar(_week())) and \
        len(events_of(db, "NEXT_CALL_SCHEDULED")) == 1                          # deduped on the event id


def test_a_meeting_that_vanished_is_dropped_unless_it_was_recorded(db, world, clock):
    calendar.sync_events(db, calendar=FakeCalendar(_week()))
    db.execute("UPDATE calendar_meetings SET call_id='call-x' WHERE event_id='adv'")
    db.commit()
    calendar.sync_events(db, calendar=FakeCalendar([_week()[0]]))
    left = sorted(r[0] for r in db.execute("SELECT event_id FROM calendar_meetings"))
    assert left == ["adv", "nwp"]           # 'int' gone from the calendar; 'adv' stays because a recording exists


def test_arm_then_the_recorder_starts_on_time_and_stops_after_grace(db, world, clock, monkeypatch):
    calendar.sync_events(db, calendar=FakeCalendar(_week()))
    assert calendar.set_record(db, "nwp", True) and not calendar.set_record(db, "nope", True)
    mgr = FakeManager()
    start = TUESDAY + timedelta(hours=3)
    assert calendar.recorder_tick(db, mgr, now_dt=start - timedelta(minutes=5)) == {"started": [], "stopped": [], "skipped": []}
    out = calendar.recorder_tick(db, mgr, now_dt=start - timedelta(seconds=30))
    assert out["started"] == ["call-1"]
    call = mgr.started[0]
    assert call["title"] == "NWP weekly" and call["deal_id"] == world.deal
    assert call["participants"] == [world.me, world.arjun]          # Arjun already known: no duplicate person
    row = db.execute("SELECT call_id, record FROM calendar_meetings WHERE event_id='nwp'").fetchone()
    assert row["call_id"] == "call-1" and row["record"] == "yes"
    # not stopped while the meeting runs or just after; stopped once the grace period has passed
    assert calendar.recorder_tick(db, mgr, now_dt=start + timedelta(minutes=35))["stopped"] == []
    assert calendar.recorder_tick(db, mgr, now_dt=start + timedelta(minutes=46))["stopped"] == ["call-1"]
    assert mgr.stopped == ["call-1"] and mgr.active is None
    # ticking again never restarts a recorded meeting
    assert calendar.recorder_tick(db, mgr, now_dt=start + timedelta(minutes=47))["started"] == []


def test_recorder_never_starts_over_a_live_call_and_records_a_missed_meeting(db, world, clock):
    calendar.sync_events(db, calendar=FakeCalendar(_week()))
    calendar.set_record(db, "nwp", True)
    calendar.set_record(db, "adv", True)
    mgr = FakeManager()
    mgr.active = "call-manual"                                 # Maya started something by hand
    start = TUESDAY + timedelta(hours=3)
    out = calendar.recorder_tick(db, mgr, now_dt=start)
    assert out["started"] == [] and out["skipped"][0]["why"].startswith("a call is already live")
    assert db.execute("SELECT record FROM calendar_meetings WHERE event_id='nwp'").fetchone()[0] == "yes"  # still armed
    mgr.active = None
    late = calendar.recorder_tick(db, mgr, now_dt=start + timedelta(hours=1))     # nwp is over by now
    assert late["started"] == [] and late["skipped"][0]["why"] == "meeting already over"
    row = db.execute("SELECT record, record_error FROM calendar_meetings WHERE event_id='nwp'").fetchone()
    assert row["record"] == "no" and row["record_error"].startswith("missed")


def test_a_failed_start_is_recorded_once_not_retried_every_tick(db, world, clock):
    calendar.sync_events(db, calendar=FakeCalendar(_week()))
    calendar.set_record(db, "nwp", True)
    mgr = FakeManager(fail=RuntimeError("callcap: microphone permission denied"))
    start = TUESDAY + timedelta(hours=3)
    out = calendar.recorder_tick(db, mgr, now_dt=start)
    assert "microphone permission denied" in out["skipped"][0]["why"]
    row = db.execute("SELECT record, record_error FROM calendar_meetings WHERE event_id='nwp'").fetchone()
    assert row["record"] == "no" and "microphone" in row["record_error"]
    assert calendar.recorder_tick(db, mgr, now_dt=start + timedelta(seconds=20))["skipped"] == []


def test_record_now_creates_unknown_attendees_as_people(db, world, clock):
    calendar.sync_events(db, calendar=FakeCalendar(_week()))
    mgr = FakeManager()
    call_id = calendar.start_recording(db, "adv", mgr)
    people = mgr.started[0]["participants"]
    assert call_id == "call-1" and len(people) == 2 and people[0] == world.me
    new = db.execute("SELECT name, email, account_id FROM people WHERE node_id=?", (people[1],)).fetchone()
    assert (new["name"], new["email"], new["account_id"]) == ("X", "x@bcg.test", None)
    with pytest.raises(calendar.RecordingRefused):
        calendar.start_recording(db, "adv", mgr)                  # already recorded
    with pytest.raises(calendar.RecordingRefused):
        calendar.start_recording(db, "int", None)                 # no live module in this server


def test_refresh_is_queued_for_the_worker_and_synced_there(db, world, clock, monkeypatch):
    monkeypatch.setattr(calendar, "ConnectorCalendar", lambda conn=None: FakeCalendar(_week()))
    assert calendar.request_refresh(db) and not calendar.request_refresh(db)     # one per minute
    assert calendar.refresh_pending(db)
    worker.drain(db)
    assert not calendar.refresh_pending(db)
    assert db.execute("SELECT COUNT(*) FROM calendar_meetings").fetchone()[0] == 3


def test_recorder_duty_reports_when_live_capture_is_missing(db, monkeypatch):
    import salescoach.live.manager as live
    monkeypatch.setattr(live, "get_manager", lambda: (_ for _ in ()).throw(ImportError("no mlx")))
    with pytest.raises(scheduler.Unavailable):
        scheduler.run_recorder(db)


def test_calendar_pages_show_every_meeting_with_record_controls(db, world, clock, monkeypatch):
    calendar.sync_events(db, calendar=FakeCalendar(_week()))
    calendar.set_record(db, "adv", True)
    from fastapi.testclient import TestClient
    mgr = FakeManager()
    app = create_app(start_worker=False, live_factory=lambda: mgr)
    client = TestClient(app, base_url="http://127.0.0.1:8140")
    page = client.get("/calendar").text
    for text in ("NWP weekly", "Advisors", "Internal sync", "Join", "Will record ✓", "Refresh calendar", "not a deal"):
        assert text in page, text
    assert "Declined" not in page and "Cancelled" not in page
    today = client.get("/").text
    assert "NWP weekly" in today and "All meetings" in today
    headers = {"Origin": "http://127.0.0.1:8140"}
    r = client.post("/calendar/nwp/record", data={"action": "arm", "next": "/"}, headers=headers, follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"].startswith("/")
    assert db.execute("SELECT record FROM calendar_meetings WHERE event_id='nwp'").fetchone()[0] == "yes"
    r = client.post("/calendar/nwp/record", data={"action": "now", "next": "/calendar"}, headers=headers,
                    follow_redirects=False)
    assert r.headers["location"] == "/live/call-1" and mgr.started[0]["title"] == "NWP weekly"
    r = client.post("/calendar/refresh", data={"next": "/calendar"}, headers=headers, follow_redirects=False)
    assert r.status_code in (302, 303) and calendar.refresh_pending(db)
