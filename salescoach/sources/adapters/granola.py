"""Granola, as a source adapter: the existing `claude -p` connector path (sources/granola.py).

Available only while the claude CLI is installed; Granola has no public API, the connector is the
way in. Meetings the poller brings in are new calls (a follow-up is drafted); the one-off
`salescoach backfill-granola` stays the way to import old meetings as history.
"""
from datetime import datetime, timedelta, timezone

from ... import providers
from .. import granola
from . import Adapter, MeetingRef, SourceError


class GranolaAdapter(Adapter):
    kind = "granola"
    label = "Granola"
    how = ("Reads your Granola meetings through the Claude connector (needs the claude CLI signed in with the "
           "Granola connector authorised). Each check is a short Claude session, so it polls hourly by default.")
    default_poll_minutes = 60

    def configured(self) -> bool:
        return providers.claude_cli_available()

    def list_recent(self, since=None) -> list:
        week_start = datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday() + 1)
        time_range = "this_week" if since and since >= week_start else "last_30_days"
        try:
            meetings = granola.list_meetings(time_range)
        except granola.GranolaError as exc:
            raise SourceError(str(exc)) from None
        refs = []
        for m in meetings:
            started = granola.parse_date(m.get("date", ""))
            if since and started and datetime.fromisoformat(started) < since:
                continue
            refs.append(MeetingRef(ext_id=m["id"], source_ref=f"granola:{m['id']}", title=m.get("title") or "",
                                   started_at=started,
                                   emails=[p["email"] for p in m.get("participants", []) if p.get("email")]))
        return refs

    def fetch(self, ext_id: str):
        try:
            return granola.normalize(granola.fetch(ext_id), ext_id)
        except granola.GranolaError as exc:
            raise SourceError(str(exc)) from None
