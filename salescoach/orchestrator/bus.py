"""Persistent workflow bus (wf_events).

An event is written before it is handled, so a crash between the two leaves
it pending and the worker picks it up on restart. dedupe_key is UNIQUE: the
same logical event (e.g. CALL_ENDED for one call) can be published any number
of times and is stored once.

publish() does not commit; the caller commits together with whatever state
change caused the event, so the event and its cause land atomically.

High-frequency live signals (levels, transcript segments) do NOT go here;
they use the in-memory live hub. This bus is for workflow steps.

Timing is one TEXT column, not_before (UTC ISO, NULL = now): fail() sets it to
attempts x RETRY_BACKOFF_S ahead, defer() to when the caller asked for. The
claim runs under the store's row lock (FOR UPDATE SKIP LOCKED on Postgres, the
write lock on SQLite), so two workers never take the same event.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..schemas.events import Event
from ..store.stores import now

MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 30      # a failed event waits attempts x this before it is claimed again


def _later(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def publish(conn, event: Event) -> bool:
    """Store an event. Returns False when its dedupe_key was already published.

    ON CONFLICT DO NOTHING, not a caught IntegrityError: on Postgres a failed statement aborts the
    caller's open transaction, and the caller has state changes in it."""
    cur = conn.execute(
        "INSERT INTO wf_events(event_id,type,entity_id,payload,causation_id,dedupe_key,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?, 'pending', ?, ?) ON CONFLICT(dedupe_key) DO NOTHING",
        (event.event_id, event.type, event.entity_id, json.dumps(event.payload),
         event.causation_id, event.dedupe_key, event.occurred_at or now(), now()))
    return cur.rowcount > 0


def _to_event(row) -> Event:
    return Event(event_id=row["event_id"], type=row["type"], entity_id=row["entity_id"],
                 occurred_at=row["created_at"], causation_id=row["causation_id"],
                 dedupe_key=row["dedupe_key"], payload=json.loads(row["payload"] or "{}"))


def claim_next(conn) -> Optional[Event]:
    """Atomically move the oldest claimable pending event to running and return it."""
    if conn.in_transaction:
        # Nothing legitimate is pending here: handlers commit their own work and a failed handler's
        # partial writes must not ride along with the next claim.
        conn.rollback()
    try:
        row = conn.lock_rows(
            "SELECT * FROM wf_events WHERE status='pending' AND attempts < ? AND "
            "(not_before IS NULL OR not_before <= ?) ORDER BY id LIMIT 1",
            (MAX_ATTEMPTS, now()), skip_locked=True).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute("UPDATE wf_events SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                     (now(), row["id"]))
        conn.commit()
        return _to_event(row)
    except Exception:
        conn.rollback()
        raise


def complete(conn, event_id: str):
    conn.execute("UPDATE wf_events SET status='done', error=NULL, updated_at=? WHERE event_id=?",
                 (now(), event_id))
    conn.commit()


def fail(conn, event_id: str, error: str):
    """Return the event to pending until it has used MAX_ATTEMPTS, then park it as failed."""
    row = conn.execute("SELECT attempts FROM wf_events WHERE event_id=?", (event_id,)).fetchone()
    attempts = row["attempts"] if row is not None else MAX_ATTEMPTS
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    conn.execute("UPDATE wf_events SET status=?, error=?, updated_at=?, not_before=? WHERE event_id=?",
                 (status, error[:2000], now(), _later(attempts * RETRY_BACKOFF_S), event_id))
    conn.commit()
    return status


def defer(conn, event_id: str, seconds: int, reason: str):
    """Put an event back without spending an attempt (e.g. the model quota is exhausted)."""
    conn.execute("UPDATE wf_events SET status='pending', "
                 "attempts=CASE WHEN attempts > 0 THEN attempts-1 ELSE 0 END, error=?, updated_at=?, not_before=? "
                 "WHERE event_id=?", (reason[:2000], now(), _later(seconds), event_id))
    conn.commit()


def recover_running(conn):
    """On worker start: anything left 'running' died with the previous process.

    An event that has already used its attempts is parked as failed, otherwise a handler that kills
    the process (a crash inside a native library on a bad recording) would be re-claimed first on
    every start and nothing else would ever run. A recovered event waits its backoff like a failed one."""
    rows = conn.execute("SELECT id, attempts FROM wf_events WHERE status='running'").fetchall()
    for row in rows:
        failed = row["attempts"] >= MAX_ATTEMPTS
        conn.execute("UPDATE wf_events SET status=?, error=COALESCE(error, 'the worker died while handling this event'), "
                     "updated_at=?, not_before=? WHERE id=?",
                     ("failed" if failed else "pending", now(), _later(row["attempts"] * RETRY_BACKOFF_S), row["id"]))
    conn.commit()


def pending(conn):
    return conn.execute(
        "SELECT * FROM wf_events WHERE status IN ('pending','running','failed') ORDER BY id").fetchall()
