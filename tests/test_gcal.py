"""Phase 5: each rep's own Google Calendar, read-only (automation/gcal.py, calendar.calendar_for).

Pinned, against a mocked Google (httpx.MockTransport through googleauth.transport; no network):
events.list is GET primary with singleEvents/orderBy/timeMin/timeMax/timeZone on the rep's own
access token, every page, and the REST JSON maps through parse_page field by field (all-day `date`
events, outOfOffice in Google's spelling, declined, cancelled, transparent, workingLocation, a
meeting room that is not a guest, hangoutLink and conferenceData entry points, a UTC dateTime); an
answer without items is not an empty calendar; the sync token lives in user_state, the incremental
request carries none of timeMin/timeMax/orderBy, deletions and moves are applied, a deal made since
the last read is matched from stored guests, 410 means a full read, and a day-old baseline is
re-read; invalid_grant marks the grant needs_reconsent once and says "reconnect your calendar";
a 401 on a cached token refreshes once; a rep with no grant is skipped quietly (no backoff, no
error, the reason in user_state) by the duty and by the refresh handler; fill_slots reads the acting
rep's calendar in the rep's timezone and says whose calendar it read; an org-pinned timezone is only
a fallback in cloud; local mode still uses the connector. Postgres: two reps with the SAME Google
event id each get their own calendar_meetings row and neither can see the other's; the calendar
page in cloud mode hides Record and shows the Connect card until the calendar is connected.
"""
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from salescoach import config, googleauth, identity, users
from salescoach.automation import calendar, connector, gcal, scheduler
from salescoach.execution import tokens
from salescoach.orchestrator import worker
from salescoach.store.stores import get_user_state
from conftest import SELLER, seed_org_settings, write_seller
from test_p4_support import ARJUN, IST, TUESDAY, clock, events_of, make_email, world  # noqa: F401
from test_tokens import keys  # noqa: F401

CAL = list(googleauth.FEATURE_SCOPES["calendar"])
GMAIL = list(googleauth.FEATURE_SCOPES["gmail"])
WED, THU, FRI = date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)
NY = ZoneInfo("America/New_York")


def at(day, hhmm, tz=IST):
    h, m = hhmm.split(":")
    return datetime.combine(day, time(int(h), int(m)), tz)


# ---- a Google Calendar that answers like the REST API ------------------------------------------------

def gev(eid, start, minutes=30, title="Meeting", guests=(), me="accepted", me_email="maya@tessel.test", **extra):
    """One events.list item as Google returns it for the calendar owner's primary calendar."""
    end = start + timedelta(minutes=minutes)
    body = {"kind": "calendar#event", "id": eid, "status": "confirmed", "summary": title,
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Kolkata"},
            "attendees": [{"email": me_email, "self": True, "organizer": True, "responseStatus": me}]
            + [{"email": g, "displayName": g.split("@")[0].title(), "responseStatus": "needsAction"} for g in guests]}
    body.update(extra)
    return body


def _bounds(item):
    def one(raw):
        if "dateTime" in raw:
            return datetime.fromisoformat(raw["dateTime"].replace("Z", "+00:00"))
        return datetime.combine(date.fromisoformat(raw["date"]), time(0), IST)
    return one(item["start"]), one(item["end"])


class FakeGoogle:
    def __init__(self):
        self.calendars = {}          # access token -> [item]
        self.changes = {}            # access token -> [item] for the next incremental answer
        self.requests = []           # {"params", "bearer"}
        self.token_calls = []
        self.token_status, self.token_answer = 200, {"access_token": "at-new", "expires_in": 3600}
        self.forced = {}             # access token -> [(status, json)] answered first, one per request
        self.expired = set()         # sync tokens that answer 410
        self.page_size = None
        self.issue_sync_token = True
        self.minted = 0

    def _mint(self):
        self.minted += 1
        return f"sync-{self.minted}"

    def handler(self, request: httpx.Request):
        if request.url == httpx.URL(googleauth.TOKEN_URL):
            self.token_calls.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(self.token_status, json=self.token_answer)
        assert request.method == "GET", "the calendar is only ever read"
        assert str(request.url).split("?")[0] == gcal.EVENTS_URL
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        params = dict(request.url.params)
        self.requests.append({"params": params, "bearer": bearer})
        if self.forced.get(bearer):
            status, body = self.forced[bearer].pop(0)
            return httpx.Response(status, json=body)
        base = {"kind": "calendar#events", "summary": "maya@tessel.test", "timeZone": "Asia/Kolkata"}
        if "syncToken" in params:
            assert not {"timeMin", "timeMax", "orderBy"} & set(params), "cannot travel with a syncToken"
            if params["syncToken"] in self.expired:
                return httpx.Response(410, json={"error": {"code": 410, "message": "Sync token is no longer valid",
                                                           "errors": [{"reason": "fullSyncRequired"}]}})
            return httpx.Response(200, json={**base, "items": self.changes.pop(bearer, []), "nextSyncToken": self._mint()})
        lo = datetime.fromisoformat(params["timeMin"])
        hi = datetime.fromisoformat(params["timeMax"])
        items = [i for i in self.calendars.get(bearer, []) if _bounds(i)[0] < hi and _bounds(i)[1] > lo]
        size = self.page_size or int(params.get("maxResults", 250))
        first = int(params.get("pageToken") or 0)
        body = {**base, "items": items[first:first + size]}
        if first + size < len(items):
            body["nextPageToken"] = str(first + size)
        elif self.issue_sync_token:
            body["nextSyncToken"] = self._mint()
        return httpx.Response(200, json=body)


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(googleauth, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setenv(googleauth.CLIENT_ID_ENV, "cid.apps.googleusercontent.com")
    monkeypatch.setenv(googleauth.CLIENT_SECRET_ENV, "csecret")
    return fake


def grant(conn, user_id="local", scopes=CAL, email="maya@tessel.test", access=None, expires_in=3600):
    tokens.store(conn, user_id, f"1//rt-{user_id}", scopes, email, access_token=access or f"at-{user_id}",
                 expires_in=expires_in)
    conn.commit()


def rep(user_id="local", mode=identity.SERVICE, **fields):
    """An acting rep whose own profile (a users row's fields) is what seller.profile() reads."""
    row = {"email": SELLER["emails"][0], "extra_emails": SELLER["emails"][1:], "name": SELLER["name"],
           "timezone": SELLER["timezone"], "signature": SELLER["signature"], "languages": SELLER["languages"],
           **fields}
    return identity.Actor(user_id, mode, "rep", users.profile_of(row))


@pytest.fixture
def cloud(db, monkeypatch):
    """Cloud mode on the test's store, acting as a rep in a service session (as the scheduler runs a
    duty); the org half of the profile where cloud mode reads it."""
    seed_org_settings(db)
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    with identity.as_actor(db, rep()):
        yield db


def meetings(conn):
    return {r["event_id"]: dict(r) for r in conn.execute("SELECT * FROM calendar_meetings ORDER BY event_id")}


# ---- events.list and the mapping ----------------------------------------------------------------------

def test_events_list_maps_the_rest_json_field_by_field(db, keys, google):
    grant(db)
    room = {"email": "c_1889@resource.calendar.google.com", "displayName": "Board room", "resource": True,
            "responseStatus": "accepted"}
    google.calendars["at-local"] = [
        gev("meet", at(WED, "11:00"), title="NWP weekly", guests=[ARJUN], hangoutLink="https://meet.google.com/abc-defg-hij",
            conferenceData={"entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}]}),
        gev("zoom", at(WED, "15:00"), title="Advisors", guests=["x@advisors.test"],
            conferenceData={"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1-555"},
                                            {"entryPointType": "video", "uri": "https://acme.zoom.us/j/123"}]}),
        {**gev("offsite", at(WED, "00:00"), title="Team offsite"), "start": {"date": "2026-09-16"},
         "end": {"date": "2026-09-17"}},
        {**gev("away", at(THU, "00:00"), title="Away", eventType="outOfOffice"), "start": {"date": "2026-09-17"},
         "end": {"date": "2026-09-18"}},
        gev("declined", at(WED, "12:00"), title="Vendor pitch", guests=["v@vendor.test"], me="declined"),
        gev("cancelled", at(WED, "13:00"), title="Old sync", status="cancelled"),
        gev("free", at(WED, "16:00"), title="Hold (free)", transparency="transparent"),
        gev("where", at(WED, "09:00"), minutes=480, title="Office", eventType="workingLocation"),
        gev("maybe", at(WED, "17:00"), title="Tentative chat", status="tentative"),
        gev("utc", datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc), title="UTC call", guests=["u@utc.test"]),
        {**gev("roomed", at(WED, "10:00"), title="Room booked", guests=["r@room.test"]),
         "attendees": [{"email": "maya@tessel.test", "self": True, "responseStatus": "accepted"}, room,
                       {"email": "r@room.test", "responseStatus": "accepted"}]},
    ]
    start, end = at(WED, "00:00"), at(FRI, "00:00")
    got = {e.id: e for e in gcal.GoogleCalendar(db).events(start, end)}
    [req] = google.requests
    p = req["params"]
    assert req["bearer"] == "at-local" and google.token_calls == []                  # the cached token, no refresh
    assert (p["singleEvents"], p["orderBy"], p["timeZone"], p["maxResults"]) == ("true", "startTime", "Asia/Kolkata", "250")
    assert p["timeMin"] == "2026-09-16T00:00:00+05:30" and p["timeMax"] == "2026-09-18T00:00:00+05:30"
    assert "syncToken" not in p
    m = got["meet"]
    assert (m.title, m.start, m.end, m.all_day) == ("NWP weekly", at(WED, "11:00"), at(WED, "11:30"), False)
    assert m.attendees == [{"email": "maya@tessel.test", "response": "accepted", "self": True},
                           {"email": ARJUN, "response": "needsAction", "self": False}]
    assert m.meeting_url == "https://meet.google.com/abc-defg-hij" and not m.declined_by_me
    assert got["zoom"].meeting_url == "https://acme.zoom.us/j/123"                        # the video entry, not the phone
    off = got["offsite"]
    assert off.all_day and (off.start, off.end) == (at(WED, "00:00"), at(THU, "00:00"))
    assert got["away"].all_day and got["away"].event_type == "outOfOffice"
    assert got["declined"].declined_by_me and got["cancelled"].status == "cancelled"
    assert got["free"].transparency == "transparent" and got["where"].event_type == "workingLocation"
    assert got["utc"].start == datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc)
    assert [a["email"] for a in got["roomed"].attendees] == ["maya@tessel.test", "r@room.test"]   # a room is not a guest
    busy = set(calendar.busy_intervals(got.values()))
    blocked = {k for k, e in got.items() if (e.start, e.end) in busy}
    # All-day outOfOffice (Google's spelling, no OOO word in the title) blocks; an all-day offsite does not.
    assert blocked == {"meet", "zoom", "away", "maybe", "utc", "roomed"}
    calendar.sync_events(db, calendar=gcal.GoogleCalendar(db), now_dt=at(TUESDAY.date(), "11:00"))
    rows = meetings(db)
    assert set(rows) == {"meet", "zoom", "maybe", "utc", "roomed"}
    assert json.loads(rows["roomed"]["attendees"]) == ["r@room.test"]
    assert rows["meet"]["meeting_url"] == "https://meet.google.com/abc-defg-hij"


def test_every_page_is_read_and_a_bottomless_calendar_is_refused(db, keys, google, monkeypatch):
    grant(db)
    google.page_size = 2
    google.calendars["at-local"] = [gev(f"e{i}", at(WED, f"{10 + i}:00")) for i in range(5)]
    events = gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))
    assert [e.id for e in events] == ["e0", "e1", "e2", "e3", "e4"]
    assert [r["params"].get("pageToken") for r in google.requests] == [None, "2", "4"]
    monkeypatch.setattr(gcal, "MAX_PAGES", 2)
    with pytest.raises(calendar.CalendarUnavailable, match="pages"):
        gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))


@pytest.mark.parametrize("body", [{"kind": "calendar#events"}, {"items": "none"}, ["not", "an", "object"]])
def test_an_answer_without_items_is_not_an_empty_calendar(db, keys, google, body):
    grant(db)
    google.forced["at-local"] = [(200, body)]
    with pytest.raises(calendar.CalendarUnavailable, match="no events list"):
        gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))


def test_other_refusals_say_what_happened(db, keys, google):
    grant(db)
    google.forced["at-local"] = [(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}),
                                 (403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}})]
    with pytest.raises(calendar.CalendarUnavailable, match="rateLimitExceeded") as first:
        gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))
    assert not isinstance(first.value, gcal.CalendarNeedsReconnect)
    with pytest.raises(gcal.CalendarNeedsReconnect, match="reconnect your calendar"):
        gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))


# ---- incremental sync -----------------------------------------------------------------------------

def test_sync_token_incremental_moves_deletes_relinks_then_410_and_daily_full_reads(db, keys, google, world, clock):
    from salescoach import repo
    grant(db)
    now = TUESDAY
    google.calendars["at-local"] = [
        gev("m1", at(WED, "11:00"), title="NWP weekly", guests=[ARJUN]),
        gev("m2", at(THU, "15:00"), title="Internal sync", guests=["dev@tessel.test"]),
        gev("m3", at(FRI, "10:00"), title="Coffee", guests=["x@other.test"]),
        gev("m5", at(FRI, "12:00"), title="Acme intro", guests=["ravi@acme.test"]),
    ]
    cal = gcal.GoogleCalendar(db)
    found = calendar.sync_events(db, calendar=cal, now_dt=now)
    assert {f["event_id"] for f in found} == {"m1", "m2", "m3", "m5"}
    [full] = google.requests
    assert "orderBy" not in full["params"] and "syncToken" not in full["params"]      # the baseline cannot be ordered
    assert full["params"]["timeMin"] == (now - timedelta(hours=2)).isoformat(timespec="seconds")
    assert full["params"]["timeMax"] == (now + timedelta(days=7) + gcal.BASELINE_PAD).isoformat(timespec="seconds")
    state = json.loads(get_user_state(db, gcal.SYNC_KEY))
    assert state["token"] == "sync-1" and state["email"] == "maya@tessel.test"
    assert json.loads(get_user_state(db, calendar.LAST_SYNC_KEY))["mode"] == "full"
    assert len(events_of(db, "NEXT_CALL_SCHEDULED")) == 1                               # m1 is NWP's

    # A deal for Acme appears; m1 is renamed, m2 deleted, m3 moved a month out, m4 added.
    acct = repo.create_account(db, "Acme", ["acme.test"])
    acme = repo.create_deal(db, "Acme pilot", account_id=acct, stage="discovery")
    db.commit()
    google.changes["at-local"] = [
        gev("m1", at(WED, "11:00"), title="NWP weekly (plant numbers)", guests=[ARJUN]),
        {"kind": "calendar#event", "id": "m2", "status": "cancelled"},
        gev("m3", at(FRI, "10:00") + timedelta(days=30), title="Coffee", guests=["x@other.test"]),
        gev("m4", at(THU, "12:00"), title="Pricing call", guests=[ARJUN]),
    ]
    later = now + timedelta(hours=2)
    changed = calendar.sync_events(db, calendar=cal, now_dt=later)
    inc = google.requests[-1]["params"]
    assert inc["syncToken"] == "sync-1" and inc["singleEvents"] == "true"
    assert not {"timeMin", "timeMax", "orderBy"} & set(inc)
    assert {f["event_id"] for f in changed} == {"m1", "m4"}
    rows = meetings(db)
    assert set(rows) == {"m1", "m4", "m5"}
    assert rows["m1"]["title"] == "NWP weekly (plant numbers)" and rows["m1"]["deal_id"] == world.deal
    assert rows["m5"]["deal_id"] == acme                                                  # matched from stored guests
    announced = sorted(e["payload"]["event_id"] for e in events_of(db, "NEXT_CALL_SCHEDULED"))
    assert announced == ["m1", "m4", "m5"]                                                # m1 once, not again
    assert json.loads(get_user_state(db, gcal.SYNC_KEY))["token"] == "sync-2"
    assert json.loads(get_user_state(db, calendar.LAST_SYNC_KEY))["mode"] == "incremental"

    # The token expired: 410, then the window in full, and a new token.
    google.expired.add("sync-2")
    google.calendars["at-local"] = [gev("m1", at(WED, "11:00"), title="NWP weekly (plant numbers)", guests=[ARJUN]),
                                    gev("m4", at(THU, "12:00"), title="Pricing call", guests=[ARJUN])]
    n = len(google.requests)
    calendar.sync_events(db, calendar=cal, now_dt=later + timedelta(hours=1))
    gone, again = google.requests[n:]
    assert gone["params"]["syncToken"] == "sync-2" and "timeMin" in again["params"] and "syncToken" not in again["params"]
    assert set(meetings(db)) == {"m1", "m4"}                                               # m5 left the calendar
    assert json.loads(get_user_state(db, gcal.SYNC_KEY))["token"] == "sync-3"

    # A day on, the window is read in full again even with a live token (scrolled-in events are not changes).
    n = len(google.requests)
    calendar.sync_events(db, calendar=cal, now_dt=later + timedelta(hours=26))
    assert "syncToken" not in google.requests[n]["params"] and "timeMin" in google.requests[n]["params"]


def test_no_sync_token_offered_means_every_read_is_full(db, keys, google):
    grant(db)
    google.issue_sync_token = False
    google.calendars["at-local"] = [gev("m1", at(WED, "11:00"))]
    cal = gcal.GoogleCalendar(db)
    calendar.sync_events(db, calendar=cal, now_dt=TUESDAY)
    calendar.sync_events(db, calendar=cal, now_dt=TUESDAY + timedelta(hours=1))
    assert all("timeMin" in r["params"] for r in google.requests) and len(google.requests) == 2
    assert not get_user_state(db, gcal.SYNC_KEY)


# ---- the grant ------------------------------------------------------------------------------------

def test_invalid_grant_marks_needs_reconsent_once_and_says_reconnect(db, keys, google, cloud, world, clock):
    grant(db, expires_in=10)                              # inside the refresh margin: the next use refreshes
    google.token_status, google.token_answer = 400, {"error": "invalid_grant", "error_description": "Token has been revoked."}
    with pytest.raises(gcal.CalendarNeedsReconnect, match="reconnect your calendar") as err:
        gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))
    assert isinstance(err.value, calendar.CalendarUnavailable)
    row = tokens.get(db, "local")
    assert row["status"] == "needs_reconsent" and "invalid_grant" in row["last_error"]
    assert len(google.token_calls) == 1 and google.requests == []
    with pytest.raises(gcal.CalendarNeedsReconnect):                                        # no loop: Google is not asked again
        gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))
    assert len(google.token_calls) == 1
    assert gcal.connection_state(db)["status"] == "needs_reconsent"
    duty = next(d for d in scheduler.default_duties() if d.name == "calendar")
    assert duty.run(db)["skipped"].startswith("calendar needs reconnecting")
    assert json.loads(get_user_state(db, calendar.UNAVAILABLE_KEY))["code"] == "needs_reconsent"
    eid = make_email(db, world.deal, body="Hi Arjun,\n\n[SLOTS]\n\nThanks,\nMaya")
    fill = calendar.fill_slots(db, eid, now_dt=TUESDAY)
    assert fill["status"] == "unavailable" and "reconnect your calendar" in fill["reason"]
    assert "[SLOTS]" in db.execute("SELECT body FROM emails WHERE id=?", (eid,)).fetchone()[0]


def test_a_401_on_the_cached_token_refreshes_once(db, keys, google):
    grant(db, access="at-stale")
    google.forced["at-stale"] = [(401, {"error": {"errors": [{"reason": "authError"}], "status": "UNAUTHENTICATED"}})]
    google.calendars["at-new"] = [gev("m1", at(WED, "11:00"))]
    assert [e.id for e in gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))] == ["m1"]
    assert [r["bearer"] for r in google.requests] == ["at-stale", "at-new"] and len(google.token_calls) == 1
    google.forced["at-new"] = [(401, {"error": {"errors": [{"reason": "authError"}]}})] * 2
    with pytest.raises(gcal.CalendarNeedsReconnect):                                        # twice: not a loop
        gcal.GoogleCalendar(db).events(at(WED, "00:00"), at(THU, "00:00"))


def test_a_rep_without_a_calendar_grant_is_skipped_quietly(db, keys, google, cloud, world, monkeypatch):
    duty = next(d for d in scheduler.default_duties() if d.name == "calendar")
    assert duty.run(db) == {"skipped": "calendar not connected"}
    assert scheduler._run_once(duty, db) is None                                           # no backoff for anyone
    assert get_user_state(db, "automation:calendar:last_error") is None
    note = json.loads(get_user_state(db, calendar.UNAVAILABLE_KEY))
    assert (note["why"], note["code"]) == ("calendar not connected", "not_connected")
    grant(db, scopes=GMAIL)                                                                 # Gmail alone is not Calendar
    assert duty.run(db) == {"skipped": "calendar not connected"}
    assert gcal.connection_state(db) == {"status": "not_connected", "email": "maya@tessel.test"}
    assert calendar.request_refresh(db)                                                     # the page's Refresh, in the worker
    worker.drain(db)
    [ev] = events_of(db, "CALENDAR_REFRESH_REQUESTED")
    assert ev["status"] == "done" and ev["entity_id"] == "user:local"
    assert google.requests == [] and google.token_calls == []
    assert db.execute("SELECT COUNT(*) FROM calendar_meetings").fetchone()[0] == 0


def test_the_cloud_duty_syncs_the_reps_own_calendar(db, keys, google, cloud, world, clock):
    grant(db)
    google.calendars["at-local"] = [gev("m1", at(WED, "11:00"), title="NWP weekly", guests=[ARJUN])]
    duty = next(d for d in scheduler.default_duties() if d.name == "calendar")
    assert duty.run(db) == {"meetings": 1, "deal_meetings": 1, "new": 1}
    assert json.loads(get_user_state(db, calendar.LAST_SYNC_KEY))["source"] == "google_calendar"
    with pytest.raises(calendar.RecordingRefused, match="recorder"):                         # nothing to arm in cloud
        calendar.set_record(db, "m1", True)
    with pytest.raises(scheduler.Unavailable, match="cloud"):
        scheduler.run_recorder(db)


# ---- slots -----------------------------------------------------------------------------------------

def test_fill_slots_uses_the_acting_reps_calendar_and_timezone(db, keys, google, dialect, monkeypatch, seller_settings):
    """Bala works from New York: his busy Wednesday morning is in his calendar, and the times offered are
    his, in his zone, under the org's rules."""
    if dialect == "postgres":
        users.create(db, "bala@tessel.test", "Bala Iyer", user_id="u-bala", timezone="America/New_York")
        db.commit()
        who, email = "u-bala", "bala@tessel.test"
    else:                                   # one user per SQLite file: the local user, from New York
        write_seller(seller_settings, {**SELLER, "timezone": "America/New_York"})
        who, email = "local", "maya@tessel.test"
    seed_org_settings(db)
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    real = config.load
    monkeypatch.setattr(config, "load", lambda name: {**(real(name) or {}), "timezone": "Asia/Kolkata"}
                        if name == "scheduling" else real(name))          # an org-pinned zone is only a fallback
    with identity.as_user(db, who, mode=identity.INTERACTIVE):
        grant(db, who, email=email, access="at-bala")
        google.calendars["at-bala"] = [gev("busy", at(WED, "10:00", NY), minutes=240, title="Board prep",
                                           me_email=email)]
        eid = make_email(db, None, body="Hi Arjun,\n\nHappy to walk Anita through it.\n[SLOTS]\n\nThanks,\nBala")
        now = datetime(2026, 9, 11, 12, 0, tzinfo=NY)                                       # Friday noon, New York
        result = calendar.fill_slots(db, eid, now_dt=now)
        body = db.execute("SELECT body FROM emails WHERE id=?", (eid,)).fetchone()[0]
        fill = calendar.last_fill(db, eid)
    assert result["status"] == "filled", result
    assert google.requests[0]["bearer"] == "at-bala" and google.requests[0]["params"]["timeZone"] == "America/New_York"
    slots = [datetime.fromisoformat(s) for s in result["slots"]]
    for s in slots:
        local = s.astimezone(NY)
        assert local.utcoffset() == timedelta(hours=-4) and time(10, 0) <= local.time() and local.time() <= time(18, 0)
        assert not (local.date() == WED and local.time() < time(14, 15))                  # his busy morning, plus buffer
    assert "EDT" in result["sentence"] and "[SLOTS]" not in body and "EDT" in body
    assert result["source"] == fill["calendar_source"] == f"Google Calendar of {email}"


def test_an_org_pinned_timezone_is_only_a_fallback_in_cloud(db, monkeypatch):
    real = config.load
    monkeypatch.setattr(config, "load", lambda name: {**(real(name) or {}), "timezone": "Europe/London"}
                        if name == "scheduling" else real(name))
    assert calendar.scheduling()["timezone"] == "Europe/London"                            # local: the file wins, as before
    seed_org_settings(db)
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    with identity.as_actor(db, rep(timezone="America/New_York")):
        assert calendar.scheduling()["timezone"] == "America/New_York"                       # the rep's own
    with identity.as_actor(db, rep(timezone="")):
        assert calendar.scheduling()["timezone"] == "Europe/London"                          # none of their own: the org's


# ---- which calendar ---------------------------------------------------------------------------------

def test_local_mode_keeps_the_connector_and_cloud_mode_uses_the_reps_own(db, monkeypatch):
    monkeypatch.delenv(identity.MODE_ENV, raising=False)
    assert isinstance(calendar.calendar_for(db), calendar.ConnectorCalendar)

    def no_connector(conn=None, refresh=False):
        raise connector.ConnectorError("the Google Calendar connector is not loaded in claude -p")
    monkeypatch.setattr(connector, "discover", no_connector)
    with pytest.raises(scheduler.Unavailable, match="not loaded"):                        # local: backs off as before
        scheduler.run_calendar(db)
    assert calendar.recording_enabled()
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    with identity.as_actor(db, identity.LOCAL_ACTOR.as_service()):
        cal = calendar.calendar_for(db)
        assert isinstance(cal, gcal.GoogleCalendar) and cal.user_id == "local" and cal.name == "google_calendar"
        assert not calendar.recording_enabled()


def test_the_google_calendar_has_no_write_path():
    names = {n.lower() for n in set(dir(gcal)) | set(dir(gcal.GoogleCalendar))}
    for verb in (*connector.WRITE_TOOLS, "invite", "insert", "patch", "post", "put", "delete", "watch", "quickadd"):
        assert not any(verb in n for n in names), verb
    assert googleauth.FEATURE_SCOPES["calendar"] == ("https://www.googleapis.com/auth/calendar.readonly",)


# ---- two reps, one invite (Postgres: the row-level policies) --------------------------------------------

@pytest.mark.postgres_only
def test_two_reps_with_the_same_event_id_each_get_their_own_row(db, keys, google, pg_owner_after_store, monkeypatch):
    for uid, email in (("u-asha", "asha@tessel.test"), ("u-bala", "bala@tessel.test")):
        users.create(db, email, uid[2:].title(), user_id=uid)
    db.commit()
    seed_org_settings(pg_owner_after_store)
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    for uid, email, title in (("u-asha", "asha@tessel.test", "Northwind review (Asha)"),
                              ("u-bala", "bala@tessel.test", "Northwind review (Bala)")):
        with identity.as_user(db, uid, mode=identity.SERVICE):
            grant(db, uid, email=email, access=f"at-{uid}")
            soon = (datetime.now(IST) + timedelta(days=1)).replace(microsecond=0)
            google.calendars[f"at-{uid}"] = [gev("shared-1", soon, title=title, guests=[ARJUN], me_email=email)]
            assert calendar.sync_own_calendar(db) == {"meetings": 1, "deal_meetings": 0, "new": 0}
    both = pg_owner_after_store.execute("SELECT owner_id, event_id, title FROM calendar_meetings ORDER BY owner_id").fetchall()
    assert [tuple(r) for r in both] == [("u-asha", "shared-1", "Northwind review (Asha)"),
                                        ("u-bala", "shared-1", "Northwind review (Bala)")]
    for uid, other in (("u-asha", "u-bala"), ("u-bala", "u-asha")):
        with identity.as_user(db, uid):
            seen = db.execute("SELECT owner_id, title FROM calendar_meetings").fetchall()     # no WHERE: the policy decides
            assert [r["owner_id"] for r in seen] == [uid]
            assert [m["title"] for m in calendar.upcoming_meetings(db)] == [seen[0]["title"]]
            assert db.execute("UPDATE calendar_meetings SET title='x' WHERE owner_id=?", (other,)).rowcount == 0
            db.commit()
            assert get_user_state(db, gcal.SYNC_KEY, user_id=other) is None                   # nor the other's sync token
            state = json.loads(get_user_state(db, gcal.SYNC_KEY))
            assert state["email"] == f"{uid[2:]}@tessel.test"


@pytest.fixture
def pg_owner_after_store(db, pg_owner):
    """The owner connection once the test's schema exists (the db fixture opened it)."""
    return pg_owner


# ---- the pages in cloud mode (Postgres: cloud mode is) ------------------------------------------------

@pytest.fixture
def rep_client(db, monkeypatch):
    """A signed-in rep (u-asha) in cloud mode: the user and the session made first, then cloud mode on."""
    from fastapi.testclient import TestClient
    from salescoach import hosted, sessions
    from salescoach.web import app as app_module
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "test-secret")
    for name in ("SALESCOACH_PASSWORD", "SALESCOACH_PASSWORD_HASH", "SALESCOACH_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    users.create(db, "asha@tessel.test", "Asha Rao", role="rep", user_id="u-asha")
    db.commit()
    seed_org_settings(db)
    _sid, cookie = sessions.create(db, "u-asha")
    db.commit()
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    client = TestClient(app, base_url="http://127.0.0.1:8140", follow_redirects=False)
    client.cookies.set(hosted.COOKIE, cookie)
    return client


PAGE = {"accept": "text/html"}
POST = {"origin": "http://127.0.0.1:8140"}


@pytest.mark.postgres_only
def test_calendar_page_in_cloud_offers_connect_then_shows_the_reps_meetings_without_record(
        db, keys, google, rep_client, monkeypatch):
    page = rep_client.get("/calendar", headers=PAGE)
    assert page.status_code == 200, page.text[:500]
    text = page.text
    assert "Connect your calendar" in text and "/auth/connect/google?feature=calendar" in text
    assert "Refresh calendar" not in text and ">Record<" not in text and "Record now" not in text
    today = rep_client.get("/", headers=PAGE).text
    assert "Connect your calendar" in today and "armed ones start by themselves" not in today
    r = rep_client.post("/calendar/refresh", data={"next": "/calendar"}, headers=POST)
    assert r.status_code == 303 and "not+connected" in r.headers["location"].replace("%20", "+")

    with identity.as_user(db, "u-asha", mode=identity.SERVICE):
        grant(db, "u-asha", email="asha@tessel.test", access="at-asha")
        google.calendars["at-asha"] = [gev("adv", datetime.now(IST) + timedelta(days=1), title="Advisors sync",
                                           guests=["x@advisors.test"], me_email="asha@tessel.test",
                                           hangoutLink="https://meet.google.com/xyz")]
        assert calendar.sync_own_calendar(db)["meetings"] == 1
    text = rep_client.get("/calendar", headers=PAGE).text
    assert "Advisors sync" in text and "asha@tessel.test" in text and "Join" in text
    assert "Connect your calendar" not in text and "Refresh calendar" in text
    assert ">Record<" not in text and "Will record" not in text and "<th>Record</th>" not in text
    today = rep_client.get("/", headers=PAGE).text
    assert "Advisors sync" in today and "<th>Record</th>" not in today

    r = rep_client.post("/calendar/adv/record", data={"action": "arm", "next": "/calendar"}, headers=POST)
    assert r.status_code == 303 and "recorder" in r.headers["location"]
    with identity.as_user(db, "u-asha"):
        assert db.execute("SELECT record FROM calendar_meetings WHERE event_id='adv'").fetchone()[0] == "no"
    r = rep_client.post("/calendar/refresh", data={"next": "/calendar"}, headers=POST)
    assert r.status_code == 303 and "Google+Calendar" in r.headers["location"].replace("%20", "+")
    with identity.as_user(db, "u-asha"):
        [ev] = db.execute("SELECT entity_id, status FROM wf_events WHERE type='CALENDAR_REFRESH_REQUESTED'").fetchall()
        assert tuple(ev) == ("user:u-asha", "pending")

    with identity.as_user(db, "u-asha", mode=identity.SERVICE):
        tokens.mark(db, "u-asha", "needs_reconsent", "invalid_grant")
        db.commit()
    text = rep_client.get("/calendar", headers=PAGE).text
    assert "Reconnect your calendar" in text and "Advisors sync" in text                  # what was read stays listed
