"""Calendar, read-only: calendar-verified meeting slots for [SLOTS], and the
deal meetings coming up.

the seller's rule: never make the buyer do the scheduling work ("when works for
you?"). Propose two or three specific times that are actually free. So a draft
keeps its [SLOTS] marker, which blocks Send, until fill_slots() has read his
calendar and found free time under config/scheduling.yaml. If the calendar
cannot be read, the marker stays and the reason is recorded: a guessed time is
worse than none.

Which calendar (calendar_for): on a local install, the seller's Google Calendar
through the claude.ai connector reached by `claude -p` (connector.py, read tools
only); in cloud mode, the ACTING user's own primary Google Calendar through
their own calendar.readonly grant (gcal.py). Nothing here creates, edits or
answers an event, or sends an invite.

Slot arithmetic is in the acting user's timezone (their profile; scheduling.yaml
may pin one on a local install). Busy time is what would really block them:
declined, cancelled, free ("transparent"), working-location and birthday
entries are ignored, and an all-day event counts only if it is out-of-office.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .. import config, identity, seller
from ..execution import cadence
from ..orchestrator import bus, review
from ..schemas.events import Event
from ..store.stores import engine, now, set_user_state
from . import common, connector

ACTOR = "calendar"
TOKEN = re.compile(r"\[\s*slots\s*\]", re.I)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OOO_WORDS = re.compile(r"\b(out of office|ooo|on leave|leave|holiday|vacation)\b", re.I)
DEFAULTS = {
    "timezone": None, "never_before": "09:00", "working_hours": {"start": "10:00", "end": "18:30"},
    "avoid": [{"start": "13:00", "end": "14:00"}], "min_notice_business_days": 1, "meeting_minutes": 30,
    "buffer_minutes": 15, "horizon_business_days": 5, "preferred_weekdays": ["Tue", "Wed", "Thu"],
    "slots_to_propose": 3, "grid_minutes": 30, "anchor_times": ["11:00", "15:30"],
}


class CalendarUnavailable(RuntimeError):
    code = "unavailable"


def kind(event_type) -> str:
    """An eventType in one spelling: Google's REST API says "outOfOffice", the connector has been seen
    to say "OUT_OF_OFFICE". Both become "outofoffice"."""
    return str(event_type or "default").replace("_", "").lower()


@dataclass
class CalEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    attendees: list = field(default_factory=list)      # [{email, response, self}]
    status: str = "confirmed"
    event_type: str = "DEFAULT"
    transparency: str = "opaque"
    declined_by_me: bool = False
    meeting_url: str | None = None                     # Meet / Zoom / Teams link, if the event has one


def scheduling(overrides: dict | None = None) -> dict:
    return _with_zone({**DEFAULTS, **_org_rules(), **(overrides or {})})


def _org_rules() -> dict:
    """scheduling.yaml (or, in cloud, the org's scheduling settings). In cloud mode a pinned timezone is
    only the fallback for a rep whose profile has none: the org's rules are shared, the clock is each rep's."""
    rules = dict(config.load("scheduling") or {})
    if identity.cloud() and rules.get("timezone") and seller.user_profile().get("timezone"):
        rules.pop("timezone")
    return rules


def own_domains() -> set[str]:
    """Domains never matched to a deal account: automation.yaml calendar.own_domains when set (an
    override), else the seller profile's."""
    return {d.lower() for d in (common.cfg("calendar").get("own_domains") or seller.own_domains())}


def _with_zone(cfg: dict) -> dict:
    """scheduling.yaml may pin a timezone; otherwise meeting times are proposed in the seller's own."""
    return cfg if cfg.get("timezone") else {**cfg, "timezone": seller.timezone()}


# ---- reading events --------------------------------------------------------------

MAX_PAGES = 4


def _zone(name, default):
    try:
        return ZoneInfo(name) if name else default
    except (ValueError, KeyError, OSError):          # ZoneInfoNotFoundError is a KeyError
        return default


def _when(raw) -> dict:
    """A start/end as Google returns it ({dateTime} or {date}); a bare string is accepted too."""
    if isinstance(raw, str):
        return {"dateTime": raw} if "T" in raw else {"date": raw}
    return raw if isinstance(raw, dict) else {}


def _local(raw, tz) -> datetime | None:
    """A dateTime; one without an offset is in its own timeZone (or the calendar's), never UTC."""
    try:
        value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value


_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_MEETING_HOSTS = ("meet.google.", "zoom.", "teams.", "webex.", "whereby.", "around.co", "meet.")


def _meeting_url(e: dict) -> str | None:
    """The join link: hangoutLink, a video entry point, or a meeting URL in the location or description."""
    if e.get("hangoutLink"):
        return str(e["hangoutLink"])
    for ep in (e.get("conferenceData") or {}).get("entryPoints") or []:
        if isinstance(ep, dict) and ep.get("uri") and ep.get("entryPointType") in (None, "video"):
            return str(ep["uri"])
    for field_name in ("location", "description"):
        for m in _URL.finditer(str(e.get(field_name) or "")):
            if any(host in m.group(0).lower() for host in _MEETING_HOSTS):
                return m.group(0)
    return None


def parse_page(text: str) -> tuple[list[CalEvent], str | None]:
    """One page of Google Calendar list_events JSON (as the connector returns it) -> (events, nextPageToken).

    An answer without an events list raises: treating an unrecognised answer as
    an empty calendar would offer times that are really taken.
    """
    raw = text or ""
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        raise CalendarUnavailable("the calendar answer had no JSON in it")
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[min(starts):])
    except json.JSONDecodeError as exc:
        raise CalendarUnavailable(f"the calendar answer is not JSON: {exc}") from exc
    if isinstance(data, list):
        items, tzname, token = data, None, None
    elif isinstance(data, dict) and ("events" in data or "items" in data):
        items = data["events"] if "events" in data else data["items"]
        tzname, token = data.get("timeZone"), data.get("nextPageToken")
    else:
        raise CalendarUnavailable("the calendar answer has no events list; not assuming the calendar is empty")
    if not isinstance(items, list):
        raise CalendarUnavailable("the calendar answer's events are not a list")
    tz = _zone(tzname, common.IST)
    out = []
    for e in items:
        if not isinstance(e, dict):
            continue
        s, en = _when(e.get("start")), _when(e.get("end"))
        all_day = "dateTime" not in s and "date" in s
        if all_day:
            try:
                first = date.fromisoformat(str(s["date"])[:10])
                last = date.fromisoformat(str(en.get("date") or s["date"])[:10])
            except ValueError:
                continue
            st = datetime.combine(first, time(0), tz)
            et = datetime.combine(last if last > first else first + timedelta(days=1), time(0), tz)
        else:
            st = _local(s.get("dateTime"), _zone(s.get("timeZone"), tz))
            et = _local(en.get("dateTime"), _zone(en.get("timeZone"), tz))
            if st is None:
                continue
            et = et if et and et > st else st + timedelta(minutes=30)
        attendees = [{"email": (a.get("email") or "").lower(), "response": a.get("responseStatus"),
                      "self": bool(a.get("self"))} for a in e.get("attendees") or []
                     if isinstance(a, dict) and not a.get("resource")]      # a meeting room is not a guest
        out.append(CalEvent(
            id=str(e.get("id") or ""), title=e.get("summary") or "", start=st, end=et, all_day=all_day,
            attendees=attendees, status=e.get("status") or "confirmed", event_type=e.get("eventType") or "DEFAULT",
            transparency=e.get("transparency") or "opaque",
            declined_by_me=any(a["self"] and a["response"] == "declined" for a in attendees),
            meeting_url=_meeting_url(e)))
    return out, (token or None)


def parse_events(text: str) -> list[CalEvent]:
    return parse_page(text)[0]


def busy_intervals(events) -> list[tuple[datetime, datetime]]:
    busy = []
    for e in events:
        if e.status == "cancelled" or e.declined_by_me or e.transparency == "transparent":
            continue
        if kind(e.event_type) in ("workinglocation", "birthday"):
            continue
        if e.all_day and not (kind(e.event_type) == "outofoffice" or OOO_WORDS.search(e.title or "")):
            continue
        busy.append((e.start, e.end))
    return busy


class ConnectorCalendar:
    """The seller's primary Google Calendar through the claude.ai connector. Read-only."""

    name = "google_calendar"

    def __init__(self, conn=None, timeout: int = 180):
        self.conn, self.timeout = conn, timeout

    def available(self) -> bool:
        try:
            connector.discover(self.conn)
            return True
        except connector.ConnectorError:
            return False

    def events(self, start: datetime, end: datetime) -> list[CalEvent]:
        args = {"startTime": start.isoformat(timespec="seconds"), "endTime": end.isoformat(timespec="seconds"),
                "timeZone": seller.timezone(), "orderBy": "startTime", "pageSize": 250}
        key = f"events:{start.isoformat(timespec='minutes')}:{end.isoformat(timespec='minutes')}"
        try:
            info = connector.discover(self.conn)
            try:
                text = connector.call_read_tool(info, "list_events", args, self.timeout)
            except connector.ConnectorError:
                info = connector.discover(self.conn, refresh=True)     # tool names may have moved
                text = connector.call_read_tool(info, "list_events", args, self.timeout)
            events, token = parse_page(text)
            pages = 1
            while token:                   # a partial calendar would offer times that are taken
                if pages >= MAX_PAGES:
                    raise CalendarUnavailable(f"more than {MAX_PAGES} pages of events; not guessing from a part")
                more, token = parse_page(connector.call_read_tool(info, "list_events", {**args, "pageToken": token},
                                                                  self.timeout))
                events += more
                pages += 1
        except (connector.ConnectorError, CalendarUnavailable) as exc:
            self._cache(key, [], str(exc))
            raise CalendarUnavailable(str(exc)) from exc
        self._cache(key, [{"id": e.id, "title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat()}
                          for e in events], None)
        return events

    def _cache(self, key, events, error):
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT INTO calendar_cache(key,fetched_at,source,events,error,owner_id) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(owner_id,key) DO UPDATE SET fetched_at=excluded.fetched_at, events=excluded.events, "
            "error=excluded.error", (key, now(), self.name, json.dumps(events), error, _owner(self.conn)))
        self.conn.commit()


def calendar_for(conn):
    """The calendar of the user bound to `conn`: their own Google Calendar through their own grant in
    cloud mode (gcal.GoogleCalendar), the machine's claude.ai connector on a local install."""
    if identity.cloud():
        from .gcal import GoogleCalendar
        return GoogleCalendar(conn)
    return ConnectorCalendar(conn)


def source_label(calendar) -> str:
    """Whose calendar was read, for the draft's verification line: "Google Calendar of rep@org" for a
    rep's own calendar, the calendar's name otherwise."""
    return getattr(calendar, "label", None) or getattr(calendar, "name", "calendar")


# ---- slots -------------------------------------------------------------------------

def candidate_days(now_local: datetime, cfg: dict) -> list[date]:
    notice = int(cfg["min_notice_business_days"])
    first = cadence.add_business_days(now_local.date(), notice) if notice > 0 else now_local.date()
    days, d = [], first
    while len(days) < int(cfg["horizon_business_days"]):
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def search_window(now: datetime, cfg: dict | None = None) -> tuple[datetime, datetime]:
    cfg = scheduling() if cfg is None else _with_zone({**DEFAULTS, **cfg})
    tz = ZoneInfo(cfg["timezone"])
    days = candidate_days(now.astimezone(tz), cfg)
    return datetime.combine(days[0], time(0), tz), datetime.combine(days[-1] + timedelta(days=1), time(0), tz)


def _overlaps(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def compute_slots(busy, now: datetime, cfg: dict | None = None) -> list[datetime]:
    """Two or three free meeting starts, IST, honouring every scheduling rule.

    One slot per day, preferred weekdays first, each placed as close as the
    free time allows to alternating anchor times (a late morning, a mid
    afternoon), so the choice reads like a person picked it.
    """
    cfg = scheduling() if cfg is None else _with_zone({**DEFAULTS, **cfg})
    tz = ZoneInfo(cfg["timezone"])
    now_local = now.astimezone(tz)
    length = timedelta(minutes=int(cfg["meeting_minutes"]))
    buffer = timedelta(minutes=int(cfg["buffer_minutes"]))
    grid = int(cfg["grid_minutes"])
    hours = cfg["working_hours"]
    floor = max(common.hhmm(hours["start"]), common.hhmm(cfg["never_before"]))
    ceiling = common.hhmm(hours["end"])
    avoid = [(common.hhmm(a["start"]), common.hhmm(a["end"])) for a in cfg.get("avoid") or []]
    blocked = [(s - buffer, e + buffer) for s, e in busy]
    days = candidate_days(now_local, cfg)
    free = {}
    for day in days:
        start = datetime.combine(day, floor, tz)
        offset = (start.hour * 60 + start.minute) % grid
        if offset:
            start += timedelta(minutes=grid - offset)
        end_of_day = datetime.combine(day, ceiling, tz)
        found, t = [], start
        while t + length <= end_of_day:
            e = t + length
            if (t > now_local
                    and not any(_overlaps(t, e, datetime.combine(day, a0, tz), datetime.combine(day, a1, tz))
                                for a0, a1 in avoid)
                    and not any(_overlaps(t, e, b0, b1) for b0, b1 in blocked)):
                found.append(t)
            t += timedelta(minutes=grid)
        free[day] = found
    preferred = {WEEKDAYS.index(d) for d in cfg.get("preferred_weekdays") or []}
    want = min(3, max(2, int(cfg["slots_to_propose"])))
    anchors = [common.hhmm(a) for a in cfg.get("anchor_times") or ["11:00", "15:30"]]
    chosen = []
    for day in sorted(days, key=lambda d: (d.weekday() not in preferred, d)):
        if len(chosen) >= want:
            break
        if not free[day]:
            continue
        target = datetime.combine(day, anchors[len(chosen) % len(anchors)], tz)
        chosen.append(min(free[day], key=lambda s: (abs((s - target).total_seconds()), s)))
    return sorted(chosen)


def slot_label(slot: datetime) -> str:
    local = slot.astimezone(common.IST)
    return f"{local:%a} {local.day} {local:%b} at {local:%H:%M}"


def slots_phrase(slots) -> str:
    labels = [slot_label(s) for s in slots]
    if len(labels) == 1:
        return f"{labels[0]} {seller.tz_label(slots[0])}"
    return f"{', '.join(labels[:-1])} or {labels[-1]} {seller.tz_label(slots[0])}"


def _question(slots) -> str:
    return "Either work?" if len(slots) == 2 else "Would one of those work?"


def slots_sentence(slots) -> str:
    return f"I'm free {slots_phrase(slots)}. {_question(slots)}"


def insert_slots(body: str, slots) -> str:
    """Replace [SLOTS] with verified times so the sentence still reads naturally.

    A line that is only the marker becomes a full sentence; a marker inside a
    sentence becomes the times, with the short question added after that
    sentence unless it already is one.
    """
    phrase, question = slots_phrase(slots), _question(slots)
    lines, asked = [], False
    for line in (body or "").split("\n"):
        if not TOKEN.search(line):
            lines.append(line)
            continue
        if not TOKEN.sub("", line).strip(" \t.:;,-"):
            lines.append(slots_sentence(slots) if not asked else f"I'm free {phrase}.")
            asked = True
            continue
        filled = TOKEN.sub(phrase, line)
        if not asked:
            after = filled.find(phrase) + len(phrase)
            end = re.search(r"[.!?](?=\s|$)", filled[after:])
            if end is None:
                filled = filled.rstrip() + ". " + question
            elif filled[after + end.start()] != "?":
                cut = after + end.end()
                filled = filled[:cut] + " " + question + filled[cut:]
            asked = True
        lines.append(filled)
    return "\n".join(lines)


def _record_fill(conn, email_id, status, reason=None, slots=(), sentence=None, source=None, busy_count=None,
                 verified_at=None) -> dict:
    conn.execute(
        "INSERT INTO slot_fills(email_id,status,slots,sentence,reason,calendar_source,busy_count,verified_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (email_id, status, json.dumps([s.isoformat() for s in slots]), sentence, reason, source, busy_count,
         verified_at, now()))
    return {"status": status, "reason": reason, "slots": [s.isoformat() for s in slots], "sentence": sentence,
            "verified_at": verified_at, "source": source}


def fill_slots(conn, email_id: int, calendar=None, now_dt: datetime | None = None) -> dict:
    """Replace [SLOTS] in a draft with calendar-verified times, or say why not."""
    row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if row is None:
        raise KeyError(f"email {email_id}")
    if row["status"] not in ("drafted", "failed"):
        result = _record_fill(conn, email_id, "refused", f"the email is {row['status']}; only a draft can change")
        conn.commit()
        return result
    body = row["body"] or ""
    if not TOKEN.search(body):
        result = _record_fill(conn, email_id, "not_needed", "the draft has no [SLOTS] marker")
        conn.commit()
        return result
    calendar = calendar or calendar_for(conn)
    current = (now_dt or common.now_ist()).astimezone(common.IST)
    cfg = scheduling()
    start, end = search_window(current, cfg)
    try:
        source = source_label(calendar)
        events = calendar.events(start, end)
    except Exception as exc:
        what = "calendar not connected" if getattr(exc, "code", "") == "not_connected" else "calendar unreachable"
        result = _record_fill(conn, email_id, "unavailable", f"{what}, so [SLOTS] stays: {exc}",
                              source=getattr(calendar, "name", "calendar"))
        conn.commit()
        return result
    busy = busy_intervals(events)
    slots = compute_slots(busy, current, cfg)
    if len(slots) < 2:
        result = _record_fill(conn, email_id, "no_slots",
                              f"only {len(slots)} free slot(s) in the next {cfg['horizon_business_days']} business "
                              "days under the scheduling rules; [SLOTS] stays", slots, source=source,
                              busy_count=len(busy))
        conn.commit()
        return result
    new_body = insert_slots(body, slots)
    review.update_email(conn, email_id, row["subject"], new_body, json.loads(row["to_addrs"] or "[]"),
                        json.loads(row["cc_addrs"] or "[]"))
    verified = now()
    engine._emit(conn, ACTOR, "email_slots_filled", node_id=row["call_id"] or row["deal_id"],
                 after={"email_id": email_id, "slots": [s.isoformat() for s in slots], "verified_at": verified,
                        "source": source})
    result = _record_fill(conn, email_id, "filled", None, slots, slots_sentence(slots), source, len(busy), verified)
    conn.commit()
    return result


def last_fill(conn, email_id: int):
    return conn.execute("SELECT * FROM slot_fills WHERE email_id=? ORDER BY id DESC LIMIT 1", (email_id,)).fetchone()


# ---- deal meetings ------------------------------------------------------------------

def _deal_matchers(conn) -> tuple[dict, dict]:
    own = own_domains()
    by_domain, by_email = {}, {}
    for r in conn.execute(
            "SELECT d.node_id, a.domains FROM deals d JOIN accounts a ON a.node_id=d.account_id "
            "WHERE d.status='active' ORDER BY d.updated_at DESC"):
        for dom in json.loads(r["domains"] or "[]"):
            dom = dom.lower().strip()
            if dom and dom not in own:
                by_domain.setdefault(dom, r["node_id"])
    for r in conn.execute(
            "SELECT dp.deal_id, p.email FROM deal_people dp JOIN people p ON p.node_id=dp.person_id "
            "JOIN deals d ON d.node_id=dp.deal_id WHERE d.status='active' AND p.is_me=0 AND p.email IS NOT NULL"):
        by_email.setdefault(r["email"].lower(), r["deal_id"])
    return by_domain, by_email


# ---- every meeting on the calendar, and which ones to record ------------------------------

SKIP_TYPES = {"workingLocation", "birthday", "outOfOffice", "focusTime"}
SKIP_KINDS = {kind(t) for t in SKIP_TYPES}           # compared through kind(): either spelling
MEETING_COLUMNS = {           # added after the table first shipped; ensure_columns() adds them to older databases
    "record": "TEXT NOT NULL DEFAULT 'no'", "call_id": "TEXT", "meeting_url": "TEXT",
    "last_seen_at": "TEXT", "record_error": "TEXT",
}
LAST_SYNC_KEY = "automation:calendar:last_sync"        # per user (user_state): a calendar is one rep's


def _owner(conn) -> str:
    return identity.actor_of(conn).user_id


class RecordingRefused(RuntimeError):
    pass


def ensure_columns(conn) -> None:
    if conn.dialect != "sqlite":                # the Postgres baseline already has every column
        return
    have = {r[1] for r in conn.execute("PRAGMA table_info(calendar_meetings)")}
    for col, decl in MEETING_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE calendar_meetings ADD COLUMN {col} {decl}")


def _is_meeting(e) -> bool:
    """A real meeting to show: not cancelled, declined, all-day, out-of-office, free or a marker entry."""
    if not e.id or e.status == "cancelled" or e.declined_by_me or e.all_day or kind(e.event_type) in SKIP_KINDS:
        return False
    return not (OOO_WORDS.search(e.title or "") or e.transparency == "transparent")


def _match(guests, by_domain, by_email) -> tuple[str | None, list[str]]:
    deal, domains = None, []
    for addr in guests:
        dom = addr.split("@", 1)[-1]
        match = by_email.get(addr) or by_domain.get(dom)
        if match:
            deal = deal or match
            if dom not in domains:
                domains.append(dom)
    return deal, domains


def _announce(conn, owner, event_id, deal, title, start, guests) -> None:
    """A meeting just got linked to a deal: ask for a prep brief (once per owner and event)."""
    bus.publish(conn, Event(type="NEXT_CALL_SCHEDULED", entity_id=deal, dedupe_key=f"NEXT_CALL:{owner}:{event_id}",
                            payload={"event_id": event_id, "deal_id": deal, "title": title, "start": start,
                                     "attendees": guests}))
    engine._emit(conn, ACTOR, "deal_meeting_seen", node_id=deal, after={"event_id": event_id, "title": title, "start": start})


def sync_events(conn, days: int | None = None, calendar=None, now_dt: datetime | None = None) -> list[dict]:
    """Every meeting on the calendar for the next `days`, stored in calendar_meetings so the Today and
    Calendar pages can show them all (and, on a local install, the seller can pick which to record).

    Cancelled, declined, all-day and working-location entries are skipped. A meeting whose attendees
    belong to a deal is linked to it and, the first time it is seen, publishes NEXT_CALL_SCHEDULED so a
    prep brief is written. Meetings that vanished from the calendar are dropped unless they were recorded.

    A calendar with a sync() (gcal.GoogleCalendar) may answer with only what changed since its last
    read: changed meetings are stored, deleted or no-longer-relevant ones dropped, and meetings already
    stored are matched again against today's deals (a deal made since is not a calendar change).
    Returns the meetings stored in this run (on an incremental run, the changed ones).
    """
    ensure_columns(conn)
    owner = _owner(conn)
    days = days or int(common.cfg("calendar").get("upcoming_days", 7))
    current = (now_dt or common.now_ist()).astimezone(common.IST)
    calendar = calendar or calendar_for(conn)
    window_start, window_end = current - timedelta(hours=2), current + timedelta(days=days)
    if hasattr(calendar, "sync"):
        result = calendar.sync(window_start, window_end, now_dt=current)
        events, gone, full = list(result.events), list(result.removed), result.full
    else:
        events, gone, full = calendar.events(window_start, window_end), [], True
    by_domain, by_email = _deal_matchers(conn)
    mine = common.my_addresses(conn)
    stamp = now()
    seen = datetime.now(common.IST).isoformat(timespec="microseconds")   # unique to this sync, not just this second
    found = []
    for e in events:
        if not _is_meeting(e) or not (e.start < window_end and e.end > window_start):
            if e.id:
                gone.append(e.id)                    # on a full read the sweep below drops it anyway
            continue
        guests = [a["email"] for a in e.attendees if a["email"] and not a["self"] and a["email"] not in mine]
        deal, domains = _match(guests, by_domain, by_email)
        existing = conn.execute("SELECT event_id, deal_id FROM calendar_meetings WHERE owner_id=? AND event_id=?",
                                (owner, e.id)).fetchone()
        conn.execute(
            "INSERT INTO calendar_meetings(event_id,deal_id,title,start_at,end_at,attendees,matched_domains,"
            "meeting_url,first_seen_at,last_seen_at,updated_at,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(owner_id,event_id) DO UPDATE SET deal_id=COALESCE(excluded.deal_id, calendar_meetings.deal_id), "
            "title=excluded.title, start_at=excluded.start_at, end_at=excluded.end_at, attendees=excluded.attendees, "
            "matched_domains=excluded.matched_domains, meeting_url=excluded.meeting_url, "
            "last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at",
            (e.id, deal, e.title, e.start.isoformat(), e.end.isoformat(), json.dumps(guests), json.dumps(domains),
             e.meeting_url, stamp, seen, stamp, owner))
        if deal and (existing is None or not existing["deal_id"]):
            _announce(conn, owner, e.id, deal, e.title, e.start.isoformat(), guests)
        found.append({"event_id": e.id, "deal_id": deal, "title": e.title, "start": e.start.isoformat(),
                      "end": e.end.isoformat(), "attendees": guests, "meeting_url": e.meeting_url,
                      "new": existing is None})
    if full:
        # Gone from the calendar (moved out of the window or deleted) and never recorded: drop it.
        conn.execute("DELETE FROM calendar_meetings WHERE owner_id=? AND call_id IS NULL AND start_at>=? AND start_at<=? "
                     "AND (last_seen_at IS NULL OR last_seen_at!=?)",
                     (owner, window_start.isoformat(), window_end.isoformat(), seen))
    else:
        for event_id in dict.fromkeys(gone):
            conn.execute("DELETE FROM calendar_meetings WHERE owner_id=? AND event_id=? AND call_id IS NULL "
                         "AND start_at>=?", (owner, event_id, window_start.isoformat()))
        _relink(conn, owner, window_start, window_end, by_domain, by_email)
    set_user_state(conn, LAST_SYNC_KEY, json.dumps({"at": stamp, "events": len(found), "source": calendar.name,
                                                    "mode": "full" if full else "incremental"}))
    conn.commit()
    return found


def _relink(conn, owner, window_start, window_end, by_domain, by_email) -> None:
    """Stored meetings with no deal yet, matched against today's deals from their stored guests."""
    for r in conn.execute("SELECT event_id, title, start_at, attendees FROM calendar_meetings WHERE owner_id=? "
                          "AND deal_id IS NULL AND start_at>=? AND start_at<=?",
                          (owner, window_start.isoformat(), window_end.isoformat())).fetchall():
        guests = json.loads(r["attendees"] or "[]")
        deal, domains = _match(guests, by_domain, by_email)
        if deal:
            conn.execute("UPDATE calendar_meetings SET deal_id=?, matched_domains=?, updated_at=? WHERE owner_id=? "
                         "AND event_id=?", (deal, json.dumps(domains), now(), owner, r["event_id"]))
            _announce(conn, owner, r["event_id"], deal, r["title"], r["start_at"], guests)


def upcoming_deal_meetings(conn, days: int | None = None, calendar=None, now_dt: datetime | None = None) -> list[dict]:
    """The deal-linked subset of sync_events(): what the prep pipeline cares about."""
    return [m for m in sync_events(conn, days=days, calendar=calendar, now_dt=now_dt) if m["deal_id"]]


def upcoming_meetings(conn, limit: int | None = None, now_dt: datetime | None = None) -> list[dict]:
    """Every stored meeting from two hours ago onwards, for the Today and Calendar pages."""
    ensure_columns(conn)
    current = now_dt or common.now_ist()
    horizon = current - timedelta(hours=2)
    rows = conn.execute("SELECT m.*, d.name AS deal_name, c.wf_state AS call_state FROM calendar_meetings m "
                        "LEFT JOIN deals d ON d.node_id=m.deal_id LEFT JOIN calls c ON c.node_id=m.call_id "
                        "WHERE m.owner_id=? ORDER BY m.start_at", (_owner(conn),)).fetchall()
    out = []
    for r in rows:
        start, end = common.ts(r["start_at"]), common.ts(r["end_at"])
        if not start or start < horizon:
            continue
        m = dict(r)
        m["link"] = prep_link(r)
        m["attendee_list"] = json.loads(r["attendees"] or "[]")
        m["armed"] = r["record"] == "yes"
        m["starts_in_s"] = (start - current).total_seconds()
        m["in_progress"] = bool(end) and start <= current < end
        m["ended"] = bool(end) and current >= end
        out.append(m)
    return out[:limit] if limit else out


def set_record(conn, event_id: str, on: bool) -> bool:
    """Arm (or disarm) automatic recording of one meeting. Returns False for an unknown meeting."""
    if on and not recording_enabled():
        raise RecordingRefused("recording comes from your recorder in a cloud install, not from the calendar")
    ensure_columns(conn)
    cur = conn.execute("UPDATE calendar_meetings SET record=?, record_error=NULL, updated_at=? WHERE owner_id=? AND event_id=?",
                       ("yes" if on else "no", now(), _owner(conn), event_id))
    conn.commit()
    return cur.rowcount > 0


def participants_for(conn, row) -> list[str]:
    """Person ids for a meeting: the seller plus every other attendee, created on first sight so the
    post-call pipeline has buyer-side participants with email addresses."""
    from .. import repo
    mine = common.my_addresses(conn)
    own = own_domains()
    people = [repo.ensure_me(conn)]
    for addr in json.loads(row["attendees"] or "[]"):
        addr = (addr or "").lower().strip()
        if not addr or addr in mine:
            continue
        pid = repo.find_person_by_email(conn, addr)
        if pid is None:
            dom = addr.split("@", 1)[-1]
            account = None if dom in own else repo.find_account_by_domain(conn, dom)
            name = addr.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
            pid = repo.create_person(conn, name, email=addr, account_id=account, actor=ACTOR)
        if pid not in people:
            people.append(pid)
    return people


def start_recording(conn, event_id: str, manager, lang_mode: str = "auto") -> str:
    """Start a live capture for one meeting (title, deal and attendees from the calendar entry)."""
    ensure_columns(conn)
    row = conn.execute("SELECT * FROM calendar_meetings WHERE owner_id=? AND event_id=?", (_owner(conn), event_id)).fetchone()
    if row is None:
        raise KeyError(event_id)
    if row["call_id"]:
        raise RecordingRefused(f"this meeting was already recorded as {row['call_id']}")
    if manager is None or not recording_enabled():
        raise RecordingRefused("live capture is not available in this server")
    if not seller.is_configured():
        raise RecordingRefused(seller.NOT_CONFIGURED)
    people = participants_for(conn, row)
    conn.commit()
    try:
        call_id = manager.start_call(row["title"] or "Untitled call", deal_id=row["deal_id"], lang_mode=lang_mode,
                                     participants=people)
    except Exception as exc:
        conn.execute("UPDATE calendar_meetings SET record_error=?, updated_at=? WHERE owner_id=? AND event_id=?",
                     (f"{type(exc).__name__}: {exc}"[:500], now(), _owner(conn), event_id))
        conn.commit()
        raise
    conn.execute("UPDATE calendar_meetings SET call_id=?, record_error=NULL, updated_at=? WHERE owner_id=? AND event_id=?",
                 (call_id, now(), _owner(conn), event_id))
    engine._emit(conn, ACTOR, "meeting_recording_started", node_id=call_id,
                 after={"event_id": event_id, "title": row["title"], "deal_id": row["deal_id"]})
    conn.commit()
    return call_id


def recorder_tick(conn, manager, now_dt: datetime | None = None) -> dict:
    """Runs every few seconds while the server is up: start the armed meeting whose time has come, and
    stop a recording once its meeting has been over for the grace period (the seller can stop earlier)."""
    ensure_columns(conn)
    rc = common.cfg("calendar").get("record") or {}
    lead = timedelta(minutes=float(rc.get("lead_minutes", 1)))
    grace = timedelta(minutes=float(rc.get("grace_minutes", 15)))
    current = now_dt or common.now_ist()
    out = {"started": [], "stopped": [], "skipped": []}
    if manager is None:
        return {**out, "unavailable": "live capture is not available in this server"}
    status = manager.status() or {}
    active = status.get("call_id") if status.get("active") else None
    if active:
        row = conn.execute("SELECT * FROM calendar_meetings WHERE owner_id=? AND call_id=?", (_owner(conn), active)).fetchone()
        end = common.ts(row["end_at"]) if row else None
        if end and current >= end + grace:
            manager.stop_call(active)
            out["stopped"].append(active)
            active = None
    for r in conn.execute("SELECT * FROM calendar_meetings WHERE owner_id=? AND record='yes' AND call_id IS NULL "
                          "ORDER BY start_at", (_owner(conn),)).fetchall():
        start, end = common.ts(r["start_at"]), common.ts(r["end_at"])
        if not start or not end or current < start - lead:
            continue
        if current >= end:
            conn.execute("UPDATE calendar_meetings SET record='no', record_error=?, updated_at=? WHERE owner_id=? AND event_id=?",
                         ("missed: the meeting ended before the recording could start", now(), _owner(conn), r["event_id"]))
            out["skipped"].append({"event_id": r["event_id"], "why": "meeting already over"})
            continue
        if active:
            out["skipped"].append({"event_id": r["event_id"], "why": f"a call is already live ({active})"})
            continue
        try:
            call_id = start_recording(conn, r["event_id"], manager)
        except Exception as exc:
            # Not retried every tick: the reason stays on the row until the seller arms it again.
            conn.execute("UPDATE calendar_meetings SET record='no', record_error=?, updated_at=? WHERE owner_id=? AND event_id=?",
                         (f"{type(exc).__name__}: {exc}"[:500], now(), _owner(conn), r["event_id"]))
            out["skipped"].append({"event_id": r["event_id"], "why": str(exc)[:200]})
            continue
        out["started"].append(call_id)
        active = call_id
    conn.commit()
    return out


def request_refresh(conn) -> bool:
    """Ask the worker to re-read the acting user's calendar now (entity user:<id>). Deduped per minute."""
    stamp = datetime.now(common.IST).strftime("%Y%m%d%H%M")
    owner = _owner(conn)
    ok = bus.publish(conn, Event(type="CALENDAR_REFRESH_REQUESTED", entity_id=f"user:{owner}",
                                 dedupe_key=f"CAL_REFRESH:{owner}:{stamp}", payload={"requested_at": now()}))
    conn.commit()
    return ok


def refresh_pending(conn) -> bool:
    return conn.execute("SELECT 1 FROM wf_events WHERE type='CALENDAR_REFRESH_REQUESTED' AND entity_id=? AND "
                        "status IN ('pending','running')", (f"user:{_owner(conn)}",)).fetchone() is not None


UNAVAILABLE_KEY = "automation:calendar:unavailable"     # per user, as the scheduler's duty bookkeeping


def recording_enabled() -> bool:
    """Arming a meeting for live capture is a local-install feature: a cloud install has no microphone,
    and a rep's calls come from their own recorder (sources, Phase 4)."""
    return not identity.cloud()


def sync_own_calendar(conn) -> dict:
    """Cloud: sync the acting user's own Google Calendar. Never raises for a missing, dead or unreachable
    link: the reason goes to user_state (automation:calendar:unavailable, with a code the pages read) and
    the answer says it was skipped, so one rep's calendar never backs off anyone else's, and a rep who
    never connected one costs nothing but this note."""
    from .gcal import GoogleCalendar
    cal = GoogleCalendar(conn)
    link = cal.connection()
    if link["status"] != "connected":
        why = ("calendar not connected" if link["status"] == "not_connected"
               else "calendar needs reconnecting: Google no longer accepts the link")
        _note_unavailable(conn, why, link["status"])
        return {"skipped": why}
    try:
        found = sync_events(conn, calendar=cal)
    except CalendarUnavailable as exc:
        conn.rollback()
        _note_unavailable(conn, str(exc), exc.code)
        return {"skipped": str(exc)[:300]}
    deal = [f for f in found if f["deal_id"]]
    return {"meetings": len(found), "deal_meetings": len(deal), "new": sum(1 for f in deal if f["new"])}


def _note_unavailable(conn, why: str, code: str) -> None:
    set_user_state(conn, UNAVAILABLE_KEY, json.dumps({"at": now(), "why": why[:500], "code": code}))
    conn.commit()


def on_refresh(conn, event):
    if identity.cloud():
        sync_own_calendar(conn)             # "Refresh calendar" on a page: the same quiet path as the duty
        return
    sync_events(conn)


def prep_meeting(conn, event_id: str) -> str:
    """Handler body for NEXT_CALL_SCHEDULED: ask Phase 3 for a prep brief, if it exists yet."""
    row = conn.execute("SELECT * FROM calendar_meetings WHERE owner_id=? AND event_id=?", (_owner(conn), event_id)).fetchone()
    if row is None:
        return "unknown"
    try:
        from ..intel import prep as intel_prep          # Phase 3; may not be installed yet
        generate = intel_prep.generate
    except (ImportError, AttributeError):
        conn.execute("UPDATE calendar_meetings SET prep_status='unavailable', prep_error=?, updated_at=? "
                     "WHERE owner_id=? AND event_id=?", ("prep briefs (Phase 3) are not installed yet", now(), _owner(conn), event_id))
        conn.commit()
        return "unavailable"
    try:
        result = generate(conn, row["deal_id"], meeting_title=row["title"],
                          attendees=tuple(json.loads(row["attendees"] or "[]")), when=row["start_at"])
    except Exception as exc:
        conn.rollback()
        conn.execute("UPDATE calendar_meetings SET prep_status='failed', prep_error=?, updated_at=? WHERE owner_id=? AND event_id=?",
                     (f"{type(exc).__name__}: {exc}"[:1000], now(), _owner(conn), event_id))
        conn.commit()
        raise
    ref = result if isinstance(result, str) else json.dumps(result, default=str)
    conn.execute("UPDATE calendar_meetings SET prep_status='ready', prep_ref=?, prep_error=NULL, updated_at=? "
                 "WHERE owner_id=? AND event_id=?", ((ref or "")[:4000], now(), _owner(conn), event_id))
    conn.commit()
    return "ready"


def prep_link(row) -> str | None:
    """Where the brief lives: a url or id Phase 3 returned, else the deal page."""
    if row["prep_status"] != "ready":
        return None
    ref = row["prep_ref"] or ""
    try:
        data = json.loads(ref)
    except (TypeError, ValueError):
        data = ref
    if isinstance(data, dict):
        for key in ("url", "href", "path"):
            if str(data.get(key) or "").startswith("/"):
                return data[key]
    if isinstance(data, str) and data.startswith("/"):
        return data
    return f"/deals/{row['deal_id']}/prep" if row["deal_id"] else None      # Phase 3's prep page


def on_next_call(conn, event):
    prep_meeting(conn, (event.payload or {}).get("event_id"))
