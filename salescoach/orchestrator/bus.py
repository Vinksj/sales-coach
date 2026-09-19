"""Persistent workflow bus (wf_events).

An event is written before it is handled, so a crash between the two leaves
it pending and the worker picks it up on restart. dedupe_key is UNIQUE: the
same logical event (e.g. CALL_ENDED for one call) can be published any number
of times and is stored once.

publish() does not commit; the caller commits together with whatever state
change caused the event, so the event and its cause land atomically.

High-frequency live signals (levels, transcript segments) do NOT go here;
they use the in-memory live hub. This bus is for workflow steps.
"""
import json
import sqlite3
from typing import Optional

from ..schemas.events import Event
from ..store.stores import now

MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 30      # a failed event waits attempts x this before it is claimed again


def publish(conn, event: Event) -> bool:
    """Store an event. Returns False when its dedupe_key was already published."""
    try:
        conn.execute(
            "INSERT INTO wf_events(event_id,type,entity_id,payload,causation_id,dedupe_key,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?, 'pending', ?, ?)",
            (event.event_id, event.type, event.entity_id, json.dumps(event.payload),
             event.causation_id, event.dedupe_key, event.occurred_at or now(), now()))
        return True
    except sqlite3.IntegrityError:
        return False


def _to_event(row) -> Event:
    return Event(event_id=row["event_id"], type=row["type"], entity_id=row["entity_id"],
                 occurred_at=row["created_at"], causation_id=row["causation_id"],
                 dedupe_key=row["dedupe_key"], payload=json.loads(row["payload"] or "{}"))


def claim_next(conn) -> Optional[Event]:
    """Atomically move the oldest pending event to running and return it."""
    if conn.in_transaction:
        # Nothing legitimate is pending here: handlers commit their own work and a failed handler's
        # partial writes must not ride along with the next claim.
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # updated_at doubles as "not before": defer() pushes it into the future.
        row = conn.execute(
            "SELECT * FROM wf_events WHERE status='pending' AND attempts < ? AND "
            "julianday(updated_at) <= julianday('now') AND "
            "(attempts=0 OR (julianday('now') - julianday(updated_at)) * 86400 >= attempts * ?) "
            "ORDER BY id LIMIT 1", (MAX_ATTEMPTS, RETRY_BACKOFF_S)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute("UPDATE wf_events SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                     (now(), row["id"]))
        conn.execute("COMMIT")
        return _to_event(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def complete(conn, event_id: str):
    conn.execute("UPDATE wf_events SET status='done', error=NULL, updated_at=? WHERE event_id=?",
                 (now(), event_id))
    conn.commit()


def fail(conn, event_id: str, error: str):
    """Return the event to pending until it has used MAX_ATTEMPTS, then park it as failed."""
    row = conn.execute("SELECT attempts FROM wf_events WHERE event_id=?", (event_id,)).fetchone()
    status = "failed" if row is None or row["attempts"] >= MAX_ATTEMPTS else "pending"
    conn.execute("UPDATE wf_events SET status=?, error=?, updated_at=? WHERE event_id=?",
                 (status, error[:2000], now(), event_id))
    conn.commit()
    return status


def defer(conn, event_id: str, seconds: int, reason: str):
    """Put an event back without spending an attempt (e.g. the model quota is exhausted)."""
    from datetime import datetime, timedelta, timezone
    later = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    conn.execute("UPDATE wf_events SET status='pending', attempts=MAX(attempts-1, 0), error=?, updated_at=? "
                 "WHERE event_id=?", (reason[:2000], later, event_id))
    conn.commit()


def recover_running(conn):
    """On worker start: anything left 'running' died with the previous process.

    An event that has already used its attempts is parked as failed, otherwise a handler that kills
    the process (a crash inside a native library on a bad recording) would be re-claimed first on
    every start and nothing else would ever run."""
    conn.execute("UPDATE wf_events SET status=CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END, "
                 "error=COALESCE(error, 'the worker died while handling this event'), updated_at=? "
                 "WHERE status='running'", (MAX_ATTEMPTS, now()))
    conn.commit()


def pending(conn):
    return conn.execute(
        "SELECT * FROM wf_events WHERE status IN ('pending','running','failed') ORDER BY id").fetchall()
