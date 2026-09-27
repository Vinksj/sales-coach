"""The worker and the scheduler without long-lived processes: what the two serverless cron endpoints run.

On a serverless platform (serverless.py: Vercel) nothing runs between requests, so the `worker` and `scheduler`
roles (ops.py) become two HTTP endpoints that Vercel Cron calls every minute (vercel.json), each authenticated by
`Authorization: Bearer $CRON_SECRET` (the CronDoor in vercel_app.py):

  /cron/drain   drain(): the worker's claim / handle / complete-or-defer loop (orchestrator/worker.drain) on one
                connection, CLAIMING for at most SALESCOACH_CRON_DRAIN_S seconds (default 50); an event claimed
                inside that window is handled to the end (vercel.json gives the function maxDuration for it), and
                whatever is not claimed stays pending for the next call. The per-owner fairness and the advisory
                owner locks are the bus's own (orchestrator/bus.py), so two drains that overlap (a slow event
                outlasting the minute) behave exactly like two worker loops: never one owner's two events at once,
                never one event twice. A drain starts with bus.recover_running, which returns to pending only
                events whose owner lock nobody holds (a drain that died with its instance).
  /cron/tick    tick(): the scheduler's duties (followups, replies, calendar, retention, recorders, learning,
                embeddings: plugins.cron_duties()), each run once when it is due and rescheduled by the delay the
                duty's own round returns (automation/scheduler.run_round). Due times live in `state`
                (ops:cron:duty:<name>). The whole tick runs under the scheduler's leader lock
                (ops.Leader, the same pg_try_advisory_lock a scheduler process takes), so overlapping ticks, or a
                tick beside a scheduler process, never run a duty twice: the second one answers "skipped".

Both use a DEDICATED connection (never the pool, never a pooler URL) for anything that holds a session advisory
lock, with two session guards: idle_session_timeout (a function frozen or killed mid-drain leaves its session idle;
Postgres ends it, and the lock with it, after SESSION_IDLE_S) and TCP keepalives. What each call did is written
to `state` (ops:cron:drain, ops:cron:tick), which /health and the "worker" pill read instead of a heartbeat
(ops.read_heartbeats: "cron").
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from . import identity, ops
from .store import db, stores
from .store.stores import now, set_state

log = logging.getLogger("salescoach.cron")

DRAIN_BUDGET_ENV = "SALESCOACH_CRON_DRAIN_S"
TICK_BUDGET_ENV = "SALESCOACH_CRON_TICK_S"
DRAIN_BUDGET_S = 50.0                 # claim new events for this long; vercel.json's maxDuration covers the last one
TICK_BUDGET_S = 50.0                  # start no further duty after this long; the rest are due next minute
MAX_EVENTS = 500
DRAIN_KEY = "ops:cron:drain"
TICK_KEY = "ops:cron:tick"
DUTY_KEY = "ops:cron:duty:{}"
SESSION_IDLE_S = 900                  # > the longest maxDuration (800 s on Pro): a live drain is never cut off
SESSION_GUARDS = (f"SET idle_session_timeout = '{SESSION_IDLE_S}s'",
                  "SET tcp_keepalives_idle = 60", "SET tcp_keepalives_interval = 10", "SET tcp_keepalives_count = 6")


def _budget(env: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(env) or default))
    except ValueError:
        return default


def _guard(conn) -> None:
    """Session guards on a dedicated Postgres connection that will hold session advisory locks."""
    if conn.dialect != db.POSTGRES:
        return
    with conn.as_system():
        for statement in SESSION_GUARDS:
            conn.execute(statement)


def _dedicated(db_path=None):
    """A connection of its own for the drain: Postgres, not from the pool (its owner locks die with it, and a
    pooled connection outlives the request), on the schema, with the session guards; nobody bound. SQLite: the
    store as usual (one file, one writer; no advisory locks)."""
    target = db_path if db_path is not None else stores.db_path()
    if not (isinstance(target, str) and db.is_postgres_url(target)):
        with identity.activate(None):
            return stores.sales(target)
    stores.sales(target).close()                   # the once-per-process schema check (refuses a stale schema)
    conn = db.connect(target)
    conn.system = True
    if stores._pg_schema:
        conn.execute(f'SET search_path TO "{stores._pg_schema}"')
    _guard(conn)
    identity.bind(conn, None)
    return conn


def _record(db_path, key: str, body: dict) -> None:
    conn = ops._open(db_path)
    try:
        with conn.as_system():                     # 'ops:%' keys: any connection writes them, acting for nobody
            set_state(conn, key, json.dumps({"at": now(), "host": ops.hostname(), "pid": os.getpid(), **body}))
            conn.commit()
    finally:
        conn.close()


# ---- /cron/drain ---------------------------------------------------------------------------------------

def drain(db_path=None, budget_s=None, max_events: int = MAX_EVENTS) -> dict:
    """Handle pending workflow events until the claim window closes or the queue is empty. Returns what it did."""
    from .orchestrator import bus
    from .orchestrator.worker import drain as worker_drain
    budget = budget_s if budget_s is not None else _budget(DRAIN_BUDGET_ENV, DRAIN_BUDGET_S)
    started = time.monotonic()
    conn = _dedicated(db_path)
    conn.system = True                             # the claim loop runs as nobody, on purpose (bus rows only)
    try:
        bus.recover_running(conn)
        handled = worker_drain(conn, max_events=max_events, deadline=started + budget)
    finally:
        try:
            bus.release_owner_locks(conn)
        finally:
            conn.close()
    took = round(time.monotonic() - started, 2)
    stopped = "max_events" if handled >= max_events else "budget" if took >= budget else "empty"
    result = {"handled": handled, "duration_s": took, "budget_s": budget, "stopped": stopped}
    _record(db_path, DRAIN_KEY, result)
    return result


# ---- /cron/tick -----------------------------------------------------------------------------------------

class CronLeader(ops.Leader):
    """The scheduler's leader lock for one tick: the same key as a scheduler process's, on a guarded connection."""

    def _connect(self):
        conn = super()._connect()
        _guard(conn)
        return conn


def _due(db_path, name: str, moment: datetime):
    """(due, state) for a duty: due when it has never run here or its next_at has come."""
    conn = ops._open(db_path)
    try:
        with conn.as_system():
            raw = stores.get_state(conn, DUTY_KEY.format(name))
    finally:
        conn.close()
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    nxt = state.get("next_at")
    if not nxt:
        return True, state
    try:
        return datetime.fromisoformat(nxt) <= moment, state
    except ValueError:
        return True, state


def _schedule(db_path, name: str, started: datetime, delay_s: float, took_s: float) -> None:
    conn = ops._open(db_path)
    try:
        with conn.as_system():
            set_state(conn, DUTY_KEY.format(name), json.dumps({
                "last_run": started.isoformat(timespec="seconds"), "took_s": round(took_s, 2),
                "next_at": (started + timedelta(seconds=delay_s)).isoformat(timespec="seconds")}))
            conn.commit()
    finally:
        conn.close()


def tick(db_path=None, budget_s=None, duties=None, clock=None) -> dict:
    """Run every scheduler duty that is due, once, under the leader lock. Returns what ran and what was skipped."""
    from . import plugins
    from .automation import scheduler
    budget = budget_s if budget_s is not None else _budget(TICK_BUDGET_ENV, TICK_BUDGET_S)
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = time.monotonic()
    leader = CronLeader(db_path)
    if not leader.try_acquire():
        return {"skipped": "another tick (or a scheduler process) holds the scheduler lock", "ran": []}
    ran, deferred, not_due, failed = [], [], [], []
    try:
        conn = ops._open(db_path)
        try:
            ops._write_leader(conn, leader.since)
        finally:
            conn.close()
        for duty in plugins.cron_duties() if duties is None else duties:
            if time.monotonic() - started >= budget:
                deferred.append(duty.name)         # still due: the next tick starts with it
                continue
            moment = clock()
            due, _state = _due(db_path, duty.name, moment)
            if not due:
                not_due.append(duty.name)
                continue
            t0 = time.monotonic()
            try:
                delay = scheduler.run_round(duty, db_path)
            except Exception:                      # one duty's failure never costs the others their turn
                log.exception("cron tick: %s could not run a round", duty.name)
                delay = scheduler.RETRY_AFTER_ERROR_S
                failed.append(duty.name)
            _schedule(db_path, duty.name, moment, delay, time.monotonic() - t0)
            ran.append(duty.name)
    finally:
        leader.release()
    result = {"ran": ran, "not_due": not_due, "deferred": deferred, "failed": failed,
              "duration_s": round(time.monotonic() - started, 2), "budget_s": budget}
    _record(db_path, TICK_KEY, result)
    return result
