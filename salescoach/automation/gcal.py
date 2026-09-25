"""One rep's own Google Calendar, read-only, through their own grant (cloud mode).

The cloud replacement for connector.ConnectorCalendar (which reaches a machine-local claude.ai
connector through `claude -p` and so cannot exist on a server). Same interface, so calendar.py does
not care which it holds: `name`, `available()`, `events(start, end) -> [CalEvent]`. On top of that,
`sync(start, end)` reads incrementally with a sync token for calendar.sync_events.

Grant. The rep connects Calendar from /me/setup (web/auth.py, feature=calendar: scope
`calendar.readonly`, stored encrypted by execution/tokens.py). Every call here asks
tokens.access_token for a live access token (the cached one, else one refresh). invalid_grant marks
the grant needs_reconsent there, once, and surfaces here as CalendarNeedsReconnect: "reconnect your
calendar". Nothing retries in a loop.

Read-only. calendar.readonly cannot write, and this module only issues GET events.list on the
`primary` calendar. There is no method that creates, edits or answers an event.

What is read (plans/sales-coach-cloud-research.md, verified facts):
  events(start, end)   events.list(singleEvents=true, orderBy=startTime, timeMin, timeMax,
                       timeZone=<the rep's>), every page (a partial calendar would offer taken times as
                       free), normalised by calendar.parse_page: the REST JSON is the same shape the
                       connector returned (items, timeZone, nextPageToken; start/end dateTime|date;
                       attendees[].email/responseStatus/self; status, eventType, transparency,
                       hangoutLink, conferenceData.entryPoints).
  sync(start, end)     incremental. The baseline is a windowed read WITHOUT orderBy (orderBy may not
                       accompany a syncToken, and the incremental request must otherwise repeat the
                       initial one; the order does not matter, the caller keys by id); the
                       nextSyncToken of its last page is kept in user_state
                       (automation:calendar:sync_token) with the window it covers. Later reads send only
                       syncToken + singleEvents (timeMin/timeMax/orderBy cannot be combined with it) and
                       get just what changed: changed events, plus bare {id, status: cancelled} items for
                       deletions. 410 Gone = the token expired: drop it and read the window in full.
                       A full read also happens daily and whenever the wanted window runs past the
                       baseline's, because a sync token only reports CHANGES, and an old event that
                       merely scrolled into view is not a change.

Slots. freebusy.query is not used: compute_slots needs the events themselves, because the busy
rules (calendar.busy_intervals: declined, cancelled, free/transparent, working-location and birthday
entries ignored; an all-day event busy only when it is out-of-office) are finer than Google's
free/busy blocks, which count an opaque all-day event as a whole busy day.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .. import googleauth, identity, seller
from ..execution import tokens
from ..store.stores import get_user_state, set_user_state
from .calendar import CalEvent, CalendarUnavailable, parse_page

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
SYNC_KEY = "automation:calendar:sync_token"
PAGE_SIZE = 250
MAX_PAGES = 20                      # 5,000 events in one window: past that, refuse rather than guess
FULL_EVERY = timedelta(hours=24)    # re-read the window in full at least this often
BASELINE_PAD = timedelta(days=2)    # a baseline covers this much past the window it was asked for


class CalendarNotConnected(CalendarUnavailable):
    """The rep never connected Calendar (or disconnected Google)."""
    code = "not_connected"


class CalendarNeedsReconnect(CalendarUnavailable):
    """Google stopped accepting the grant, or it lacks calendar.readonly: the rep connects again."""
    code = "needs_reconsent"


class _Gone(Exception):
    """410: the sync token is no longer valid."""


RECONNECT = "reconnect your calendar from your profile page (You > Google > Calendar)"


@dataclass
class SyncResult:
    events: list = field(default_factory=list)       # [CalEvent] that exist (changed ones, or all of a full read)
    removed: list = field(default_factory=list)      # event ids deleted since the last read (incremental only)
    full: bool = True                                # True: `events` is the whole window


class GoogleCalendar:
    """The acting user's (or `user_id`'s) primary Google Calendar. Read-only."""

    name = "google_calendar"

    def __init__(self, conn, user_id: Optional[str] = None):
        self.conn = conn
        self.user_id = user_id or identity.actor_of(conn).user_id

    # ---- the grant ----------------------------------------------------------------------------

    def connection(self) -> dict:
        """{status: connected|not_connected|needs_reconsent|revoked, email}: for the pages and the duty."""
        st = tokens.status_of(self.conn, self.user_id)
        if st["status"] == "active":
            status = "connected" if "calendar" in st["features"] else "not_connected"
        elif st["status"] == "not_connected":
            status = "not_connected"
        elif "calendar" in st["features"]:
            status = st["status"]                         # needs_reconsent | revoked
        else:
            status = "not_connected"                      # a dead Gmail-only grant: Calendar was never connected
        return {"status": status, "email": st["email"]}

    @property
    def email(self) -> Optional[str]:
        return self.connection()["email"]

    @property
    def label(self) -> str:
        """Whose calendar: goes on the draft's verification line and the sync record."""
        email = self.email
        return f"Google Calendar of {email}" if email else "your Google Calendar"

    def available(self) -> bool:
        return tokens.has_feature(self.conn, self.user_id, "calendar")

    def _token(self) -> str:
        if not tokens.has_feature(self.conn, self.user_id, "calendar"):
            state = self.connection()["status"]
            if state in ("needs_reconsent", "revoked"):
                raise CalendarNeedsReconnect(f"Google no longer accepts your calendar link: {RECONNECT}")
            raise CalendarNotConnected("Google Calendar is not connected: connect it from your profile page "
                                       "(You > Google > Connect Calendar)")
        try:
            return tokens.access_token(self.conn, self.user_id)
        except tokens.NeedsReconsent as exc:              # invalid_grant: tokens marked the row, once
            raise CalendarNeedsReconnect(f"Google no longer accepts your calendar link: {RECONNECT}") from exc
        except tokens.NoToken as exc:
            raise CalendarNotConnected(str(exc)) from exc
        except tokens.TokenError as exc:
            raise CalendarUnavailable(f"could not get a Google access token: {exc}") from exc

    # ---- HTTP ---------------------------------------------------------------------------------

    def _get(self, params: dict) -> httpx.Response:
        """One events.list page. A 401 on a cached access token drops the cache and refreshes once."""
        for attempt in (1, 2):
            token = self._token()
            with googleauth.http() as client:
                try:
                    response = client.get(EVENTS_URL, params=params, headers={"Authorization": f"Bearer {token}"})
                except httpx.HTTPError as exc:
                    raise CalendarUnavailable(f"could not reach Google Calendar: {type(exc).__name__}") from exc
            if response.status_code == 401 and attempt == 1:
                tokens.forget_access_token(self.conn, self.user_id)
                continue
            break
        if response.status_code == 200:
            return response
        if response.status_code == 410:
            raise _Gone()
        reason = _reason(response)
        if response.status_code in (401, 403) and reason in ("authError", "insufficientPermissions", "forbidden",
                                                             "ACCESS_TOKEN_SCOPE_INSUFFICIENT", ""):
            raise CalendarNeedsReconnect(f"Google refused the calendar read ({response.status_code} {reason or 'denied'}): "
                                         f"{RECONNECT}")
        raise CalendarUnavailable(f"Google Calendar answered {response.status_code}"
                                  + (f" ({reason})" if reason else ""))

    def _pages(self, params: dict) -> tuple[list[CalEvent], list[str], Optional[str]]:
        """Every page of one listing -> (events, cancelled ids, the last page's nextSyncToken)."""
        events, removed, token, pages = [], [], None, 0
        while True:
            response = self._get({**params, **({"pageToken": token} if token else {})})
            pages += 1
            try:
                body = response.json()
            except ValueError as exc:
                raise CalendarUnavailable("Google Calendar's answer was not JSON") from exc
            if not isinstance(body, dict) or not isinstance(body.get("items"), list):
                raise CalendarUnavailable("Google Calendar's answer has no events list; not assuming the calendar is empty")
            removed += [str(i["id"]) for i in body["items"]
                        if isinstance(i, dict) and i.get("status") == "cancelled" and i.get("id")]
            more, token = parse_page(json.dumps(body))
            events += more
            if not token:
                return events, removed, body.get("nextSyncToken") or None
            if pages >= MAX_PAGES:
                raise CalendarUnavailable(f"more than {MAX_PAGES} pages of events; not guessing from a part")

    # ---- the interface calendar.py uses ---------------------------------------------------------

    def events(self, start: datetime, end: datetime) -> list[CalEvent]:
        """Every event between start and end, all pages. Touches no sync state."""
        events, _removed, _token = self._pages(self._window(start, end, ordered=True))
        return events

    def sync(self, start: datetime, end: datetime, now_dt: Optional[datetime] = None) -> SyncResult:
        """What changed since the last read when a valid token covers [start, end); else the window in
        full (and a new token kept). The token is written in the caller's transaction, so a sync that
        fails later is rolled back with it and the same changes come again next time."""
        current = now_dt or datetime.now(timezone.utc)
        state = self._state()
        email = self.email
        if state and self._covers(state, start, end, current, email):
            try:
                events, removed, token = self._pages(self._incremental(state["token"]))
            except _Gone:
                self._forget()                              # expired: fall through to a full read
            else:
                if token:
                    self._keep({**state, "token": token, "last_at": current.isoformat(timespec="seconds")})
                else:
                    self._forget()
                return SyncResult(events, removed, full=False)
        base_end = end + BASELINE_PAD
        try:
            events, _removed, token = self._pages(self._window(start, base_end, ordered=False))
        except _Gone as exc:                                 # not expected without a token; never a loop
            raise CalendarUnavailable("Google Calendar answered 410 to a full read") from exc
        if token:
            self._keep({"token": token, "start": start.isoformat(), "end": base_end.isoformat(), "email": email,
                        "full_at": current.isoformat(timespec="seconds"),
                        "last_at": current.isoformat(timespec="seconds")})
        else:
            self._forget()
        return SyncResult([e for e in events if e.start < end and e.end > start], [], full=True)

    # ---- helpers ------------------------------------------------------------------------------

    def _window(self, start: datetime, end: datetime, ordered: bool) -> dict:
        params = {"timeMin": _rfc3339(start), "timeMax": _rfc3339(end), "singleEvents": "true",
                  "maxResults": PAGE_SIZE, "timeZone": seller.timezone()}
        if ordered:
            params["orderBy"] = "startTime"
        return params

    @staticmethod
    def _incremental(token: str) -> dict:
        """The baseline's parameters without those a syncToken may not travel with: timeMin, timeMax and
        orderBy (q, updatedMin, iCalUID and the extended-property filters are never sent at all)."""
        return {"syncToken": token, "singleEvents": "true", "maxResults": PAGE_SIZE, "timeZone": seller.timezone()}

    def _state(self) -> Optional[dict]:
        raw = get_user_state(self.conn, SYNC_KEY, user_id=self.user_id)
        try:
            state = json.loads(raw) if raw else None
        except ValueError:
            return None
        return state if isinstance(state, dict) and state.get("token") else None

    @staticmethod
    def _covers(state: dict, start: datetime, end: datetime, current: datetime, email) -> bool:
        try:
            s, e = datetime.fromisoformat(state["start"]), datetime.fromisoformat(state["end"])
            full_at = datetime.fromisoformat(state["full_at"])
        except (KeyError, TypeError, ValueError):
            return False
        return (s <= start and end <= e and current - full_at < FULL_EVERY and current >= full_at
                and state.get("email") == email)

    def _keep(self, state: dict) -> None:
        set_user_state(self.conn, SYNC_KEY, json.dumps(state), user_id=self.user_id)

    def _forget(self) -> None:
        set_user_state(self.conn, SYNC_KEY, "", user_id=self.user_id)


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def _reason(response: httpx.Response) -> str:
    """Google's error reason ({"error": {"errors": [{"reason"}], "status"}}), or ''."""
    try:
        err = (response.json() or {}).get("error") or {}
    except ValueError:
        return ""
    if not isinstance(err, dict):
        return ""
    for item in err.get("errors") or []:
        if isinstance(item, dict) and item.get("reason"):
            return str(item["reason"])
    for detail in err.get("details") or []:
        if isinstance(detail, dict) and detail.get("reason"):
            return str(detail["reason"])
    return str(err.get("status") or "")


def connection_state(conn) -> dict:
    """The acting user's calendar link, for the pages: {status, email}."""
    return GoogleCalendar(conn).connection()
