"""Background duties for `salescoach serve` (plugins/execution.start_background).

Four threads, each with its own sales.db handle, each honouring the stop Event:
  followups   daily at followup.run_at IST (09:30), and soon after start when
              today's run was missed;
  replies     every replies.poll_minutes, only while Gmail credentials load;
  calendar    every calendar.scan_hours, only while the connector answers;
  autosend    every auto_send.interval_minutes; a no-op unless enabled;
  recorder    every calendar.record.poll_seconds: starts the meetings the seller
              armed for recording and stops them after the grace period.
Each duty waits before its first run, so a server that starts and stops at
once (a test, for example) never reaches Gmail or `claude -p`. Last run,
result, error and unavailability are recorded as automation:<duty>:*: in
user_state for a per-user duty, in state for an org-level one.

Users. A duty is per user unless it says otherwise (Duty.per_user): each round
runs it once for every active user (users.active), inside that user's service
session, so a reply poll reads that user's mailbox and a follow-up run drafts
in that user's voice. An org-level duty (the sources poller, until Phase 4)
runs once, as the local user, and not at all in cloud mode.
"""
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from .. import config, identity, seller, users
from ..store import stores
from ..store.stores import get_user_state, now, set_state, set_user_state
from . import common

log = logging.getLogger("salescoach.automation")


RETRY_AFTER_ERROR_S = 15 * 60


class Unavailable(RuntimeError):
    """A duty's dependency (Gmail, the calendar connector) is not there; back off."""

    def __init__(self, message: str, retry_s: Optional[float] = None):
        super().__init__(message)
        self.retry_s = retry_s


@dataclass
class Duty:
    name: str
    run: Callable                 # run(conn) -> json-able result
    interval_s: Callable          # () -> seconds until the next run
    first_delay_s: float = 60.0
    per_user: bool = True         # once per active user, in that user's session; False = once, org-level


def seconds_until(run_at: str, current: Optional[datetime] = None) -> float:
    current = current or common.now_ist()
    t = common.hhmm(run_at)
    target = current.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


def _record(conn, duty: Duty, key: str, value: str) -> None:
    if duty.per_user:
        set_user_state(conn, f"automation:{duty.name}:{key}", value)
    else:
        set_state(conn, f"automation:{duty.name}:{key}", value)


def _run_once(duty: Duty, conn) -> float | None:
    """One duty for the user bound to `conn`. Returns a longer delay when the duty is unavailable."""
    try:
        if not seller.is_configured():
            # No profile, no duties: without the seller's addresses a reply poll cannot tell their
            # mail from a buyer's. Checked every round, so saving the profile is enough to start.
            raise Unavailable("the seller profile is not set up yet", retry_s=60)
        result = duty.run(conn)
        _record(conn, duty, "last_run", now())
        _record(conn, duty, "last_result", json.dumps(result, default=str)[:4000])
        conn.commit()
    except Unavailable as exc:
        conn.rollback()
        _record(conn, duty, "unavailable", json.dumps({"at": now(), "why": str(exc)[:500]}))
        conn.commit()
        return exc.retry_s
    except Exception as exc:
        log.exception("%s failed", duty.name)
        try:
            conn.rollback()
            _record(conn, duty, "last_error", json.dumps({"at": now(), "error": f"{type(exc).__name__}: {exc}"[:1000]}))
            conn.commit()
        except Exception:
            pass
        return RETRY_AFTER_ERROR_S
    return None


def _loop(duty: Duty, db_path, stop: threading.Event):
    delay = duty.first_delay_s
    while not stop.wait(delay):
        delay = duty.interval_s()
        try:
            with identity.activate(None):
                conn = stores.sales(db_path)
        except Exception:
            log.exception("%s: cannot open sales.db", duty.name)
            continue
        try:
            if duty.per_user:
                targets = [u["id"] for u in users.active(conn)]
            elif identity.cloud():
                log.info("%s: an org-level duty does not run in cloud mode yet (Phase 4)", duty.name)
                continue
            else:
                targets = [identity.LOCAL_USER]
            for user_id in targets:
                if getattr(stop, "is_set", lambda: False)():   # tests pass a duck with wait() only
                    break
                try:
                    with identity.as_user(conn, user_id, mode=identity.SERVICE):
                        retry = _run_once(duty, conn)
                except identity.NoActor:
                    log.exception("%s: user %s is gone", duty.name, user_id)
                    continue
                if retry is not None:
                    # An unavailable dependency backs the whole duty off; an error must not lose the day.
                    delay = max(delay, retry) if retry > RETRY_AFTER_ERROR_S else min(delay, retry)
        except Exception:
            log.exception("%s: round failed", duty.name)
        finally:
            conn.close()


def start(db_path, stop: threading.Event, duties: Optional[list] = None) -> list[threading.Thread]:
    threads = []
    for duty in default_duties() if duties is None else duties:
        thread = threading.Thread(target=_loop, args=(duty, db_path, stop), name=f"salescoach-{duty.name}", daemon=True)
        thread.start()
        threads.append(thread)
    return threads


# ---- the four duties ------------------------------------------------------------------

def run_followups(conn) -> dict:
    from . import followup
    today = common.today_ist()
    run_at = common.cfg("followup").get("run_at", "09:30")
    if get_user_state(conn, "automation:followups:ran_for") == today.isoformat():
        return {"skipped": "already ran today"}
    if common.now_ist().time() < common.hhmm(run_at):
        return {"skipped": f"waiting for {run_at} {seller.tz_label()}"}
    results = followup.evaluate_due(conn, today)
    set_user_state(conn, "automation:followups:ran_for", today.isoformat())
    return {"decisions": len(results)}


def _gmail():
    """The machine-local mailbox of a local install (tests replace this)."""
    from ..execution.gmail import GmailProvider
    return GmailProvider((config.load("policy").get("email") or {}).get("account", "work"))


def gmail_for(conn):
    """The mailbox of the user bound to `conn`: their own grant in cloud mode (execution/gmail
    .provider_for; raises tokens.NoToken / NeedsReconsent), the local alias otherwise."""
    if identity.cloud():
        from ..execution import gmail
        return gmail.provider_for(conn)
    return _gmail()


def _replies_duty() -> Duty:
    held = {}

    def run(conn):
        from . import replies
        from ..execution import tokens
        if identity.cloud():
            # Per user, per round: the grant is the user's and may have died since the last round.
            try:
                gmail = gmail_for(conn)
            except tokens.TokenError as exc:
                return {"skipped": f"Gmail not connected: {exc}"[:300]}
            return replies.poll(conn, gmail)
        if "gmail" not in held:
            try:
                gmail = _gmail()
                gmail.profile()                    # read-only: proves the credentials load
            except Exception as exc:
                raise Unavailable(f"Gmail credentials did not load: {type(exc).__name__}: {exc}", retry_s=3600)
            held["gmail"] = gmail
        return replies.poll(conn, held["gmail"])

    minutes = lambda: float(common.cfg("replies").get("poll_minutes", 15)) * 60
    return Duty("replies", run, minutes, first_delay_s=60)


def run_calendar(conn) -> dict:
    from . import calendar, connector
    try:
        connector.discover(conn)
    except connector.ConnectorError as exc:
        raise Unavailable(str(exc), retry_s=6 * 3600)
    try:
        found = calendar.sync_events(conn)
    except calendar.CalendarUnavailable as exc:
        raise Unavailable(str(exc), retry_s=6 * 3600)
    deal = [f for f in found if f["deal_id"]]
    return {"meetings": len(found), "deal_meetings": len(deal), "new": sum(1 for f in deal if f["new"])}


def run_recorder(conn) -> dict:
    """Start armed meetings on time and stop them after the grace period. Needs the live module."""
    from . import calendar
    if identity.cloud():
        raise Unavailable("live capture is not available in a cloud install", retry_s=24 * 3600)
    try:
        from ..live.manager import get_manager
        manager = get_manager()
    except Exception as exc:
        raise Unavailable(f"live capture is not available: {type(exc).__name__}: {exc}", retry_s=600)
    return calendar.recorder_tick(conn, manager)


def run_autosend(conn) -> dict:
    from . import autosend
    return autosend.run_once(conn, lambda: gmail_for(conn))


def default_duties() -> list[Duty]:
    run_at = lambda: seconds_until(common.cfg("followup").get("run_at", "09:30"))
    return [
        Duty("followups", run_followups, run_at, first_delay_s=45),
        _replies_duty(),
        Duty("calendar", run_calendar, lambda: float(common.cfg("calendar").get("scan_hours", 2)) * 3600,
             first_delay_s=90),
        Duty("autosend", run_autosend, lambda: float(common.cfg("auto_send").get("interval_minutes", 10)) * 60,
             first_delay_s=120),
        Duty("recorder", run_recorder,
             lambda: float((common.cfg("calendar").get("record") or {}).get("poll_seconds", 20)), first_delay_s=20),
    ]
