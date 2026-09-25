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
attempts x RETRY_BACKOFF_S ahead, defer() to when the caller asked for.

Order and fairness (Phase 6). Every event carries a priority (claimed highest
first, then oldest: an interactive request, PRIORITY_INTERACTIVE, goes ahead of
an import, and a history backfill, PRIORITY_BACKFILL, behind everything) and an
owner (bus.owner_for: `user:<id>` entities, the entity's node, payload.owner_id,
else the acting user). One owner has ONE running event at a time, so a rep's
bulk import cannot take every worker while another rep waits for a redraft, and
an owner's events are handled in id order (a deal has one owner, so a deal's
events are handled in order too). How that is made race-free differs by backend:
  * Postgres: the claimer takes a per-owner SESSION advisory lock
    (pg_try_advisory_lock) BEFORE marking the row running and holds it until the
    event is settled (complete / fail / defer). Advisory locks are atomic across
    sessions, so two claimers racing for one owner's events cannot both win; the
    row itself is taken with an UPDATE ... WHERE status='pending' whose rowcount
    says whether we got it. No FOR UPDATE is needed for that.
  * SQLite: one process (docs/deploy-cloud.md); the claim runs under the write
    lock (BEGIN IMMEDIATE), which serialises claimers, so a NOT EXISTS on running
    events of the same owner is race-free there.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import identity
from ..schemas.events import Event
from ..store import db
from ..store.stores import now

log = logging.getLogger("salescoach.bus")

MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 30      # a failed event waits attempts x this before it is claimed again
CLAIM_SCAN = 50           # Postgres: how many claimable candidates one claim looks at before giving up

PRIORITY_INTERACTIVE = 10    # a person pressed a button and is waiting (redraft, retry, strategy, prep, coach)
PRIORITY_NORMAL = 0          # a new call, an import, the daily duties
PRIORITY_BACKFILL = -10      # old history: analysed when nothing else is waiting


def _later(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def owner_for(conn, event: Event) -> Optional[str]:
    """Whose work an event is, for the fairness rule: `user:<id>` entities say so, a call/deal/loop id
    resolves through nodes.owner_id, the payload may carry owner_id, else the user acting through the
    connection (None when nobody is, in cloud mode). The same order workflow.owner_of_event uses when
    the event is handled, plus the actor fallback."""
    entity = event.entity_id or ""
    if entity.startswith("user:"):
        return entity[len("user:"):]
    if entity:
        from .. import repo
        owner = repo.owner_of(conn, entity)      # Postgres: app_owner_of, whoever publishes (store/rls.py)
        if owner:
            return owner
    owner = (event.payload or {}).get("owner_id")
    if owner:
        return str(owner)
    actor = getattr(conn, "actor", None) or identity.current_actor(required=False)
    return actor.user_id if actor is not None else None


def publish(conn, event: Event, priority: int = PRIORITY_NORMAL) -> bool:
    """Store an event. Returns False when its dedupe_key was already published.

    ON CONFLICT DO NOTHING, not a caught IntegrityError: on Postgres a failed statement aborts the
    caller's open transaction, and the caller has state changes in it.

    The bus is the one table every connection may write (store/rls.py), and the worker handles an event
    AS its owner. So a person at the keyboard (interactive mode) may only queue work on their own objects:
    a manager who can read a rep's call cannot queue its redraft (manager/access.ReadOnly). Background
    duties publish as the owner they run for; nothing else is refused here. Cloud mode only (a local
    install has one user; its CLI and tests queue work for any owner id they like)."""
    owner = owner_for(conn, event)
    if identity.cloud():
        actor = getattr(conn, "actor", None) or identity.current_actor(required=False)
        if (actor is not None and actor.mode == identity.INTERACTIVE and owner is not None
                and owner != actor.user_id):
            from ..manager.access import ReadOnly
            raise ReadOnly(owner, "this")
    cur = conn.execute(
        "INSERT INTO wf_events(event_id,type,entity_id,payload,causation_id,dedupe_key,status,created_at,updated_at,"
        "priority,owner) VALUES (?,?,?,?,?,?, 'pending', ?, ?, ?, ?) ON CONFLICT(dedupe_key) DO NOTHING",
        (event.event_id, event.type, event.entity_id, json.dumps(event.payload),
         event.causation_id, event.dedupe_key, event.occurred_at or now(), now(), int(priority), owner))
    return cur.rowcount > 0


def _to_event(row) -> Event:
    return Event(event_id=row["event_id"], type=row["type"], entity_id=row["entity_id"],
                 occurred_at=row["created_at"], causation_id=row["causation_id"],
                 dedupe_key=row["dedupe_key"], payload=json.loads(row["payload"] or "{}"))


CLAIMABLE = "status='pending' AND attempts < ? AND (not_before IS NULL OR not_before <= ?)"


def claim_next(conn) -> Optional[Event]:
    """Atomically move the best claimable pending event (highest priority, then oldest, whose owner has
    nothing running) to running and return it."""
    if conn.in_transaction:
        # Nothing legitimate is pending here: handlers commit their own work and a failed handler's
        # partial writes must not ride along with the next claim.
        conn.rollback()
    try:
        if conn.dialect == db.POSTGRES:
            return _claim_postgres(conn)
        return _claim_sqlite(conn)
    except Exception:
        conn.rollback()
        raise


def _claim_sqlite(conn) -> Optional[Event]:
    row = conn.lock_rows(
        f"SELECT * FROM wf_events e WHERE {CLAIMABLE} AND NOT EXISTS (SELECT 1 FROM wf_events r "
        "WHERE r.status='running' AND COALESCE(r.owner, '') = COALESCE(e.owner, '')) "
        "ORDER BY priority DESC, id LIMIT 1",
        (MAX_ATTEMPTS, now()), skip_locked=True).fetchone()
    if row is None:
        conn.commit()
        return None
    conn.execute("UPDATE wf_events SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                 (now(), row["id"]))
    conn.commit()
    return _to_event(row)


# ---- Postgres: the per-owner advisory lock ---------------------------------------------------------

def _owner_key(owner) -> str:
    return f"salescoach:owner:{owner or ''}"


def _held(conn) -> set:
    """The owner keys this connection's session holds (one per event being handled, normally one)."""
    held = getattr(conn, "_bus_owner_locks", None)
    if held is None:
        held = set()
        conn._bus_owner_locks = held
    return held


def _try_owner_lock(conn, key: str) -> bool:
    held = _held(conn)
    if key in held:
        return True                      # re-entrant for this session; never taken twice
    got = conn.execute("SELECT pg_try_advisory_lock(hashtext(?))", (key,)).fetchone()[0]
    if got:
        held.add(key)
    return bool(got)


def _release_owner_lock(conn, key: str) -> None:
    held = _held(conn)
    if key in held:
        held.discard(key)
        conn.execute("SELECT pg_advisory_unlock(hashtext(?))", (key,))


def release_owner_locks(conn) -> None:
    """Every owner lock this connection holds, before it is closed or returned to a pool: a session
    lock outlives the wrapper and would otherwise travel with the pooled connection."""
    if conn.dialect != db.POSTGRES:
        return
    for key in list(_held(conn)):
        try:
            _release_owner_lock(conn, key)
        except Exception:
            log.exception("could not release owner lock %s", key)
            _held(conn).discard(key)


def _release_for_event(conn, event_id: str) -> None:
    if conn.dialect != db.POSTGRES or not _held(conn):
        return
    row = conn.execute("SELECT owner FROM wf_events WHERE event_id=?", (event_id,)).fetchone()
    _release_owner_lock(conn, _owner_key(row["owner"] if row is not None else None))


def _claim_postgres(conn) -> Optional[Event]:
    candidates = conn.execute(
        f"SELECT id, owner FROM wf_events WHERE {CLAIMABLE} ORDER BY priority DESC, id LIMIT ?",
        (MAX_ATTEMPTS, now(), CLAIM_SCAN)).fetchall()
    busy = set()
    for cand in candidates:
        key = _owner_key(cand["owner"])
        if key in busy:
            continue
        if not _try_owner_lock(conn, key):
            busy.add(key)                # another session is handling this owner's event: theirs come later
            continue
        cur = conn.execute("UPDATE wf_events SET status='running', attempts=attempts+1, updated_at=? "
                           "WHERE id=? AND status='pending'", (now(), cand["id"]))
        if cur.rowcount == 0:            # settled or re-timed by someone since we listed it
            conn.rollback()
            _release_owner_lock(conn, key)
            continue
        row = conn.execute("SELECT * FROM wf_events WHERE id=?", (cand["id"],)).fetchone()
        conn.commit()
        return _to_event(row)
    return None


# ---- settling ---------------------------------------------------------------------------------------

def complete(conn, event_id: str):
    conn.execute("UPDATE wf_events SET status='done', error=NULL, updated_at=? WHERE event_id=?",
                 (now(), event_id))
    conn.commit()
    _release_for_event(conn, event_id)


def fail(conn, event_id: str, error: str):
    """Return the event to pending until it has used MAX_ATTEMPTS, then park it as failed."""
    row = conn.execute("SELECT attempts FROM wf_events WHERE event_id=?", (event_id,)).fetchone()
    attempts = row["attempts"] if row is not None else MAX_ATTEMPTS
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    conn.execute("UPDATE wf_events SET status=?, error=?, updated_at=?, not_before=? WHERE event_id=?",
                 (status, error[:2000], now(), _later(attempts * RETRY_BACKOFF_S), event_id))
    conn.commit()
    _release_for_event(conn, event_id)
    return status


def defer(conn, event_id: str, seconds: int, reason: str):
    """Put an event back without spending an attempt (e.g. the model quota is exhausted)."""
    conn.execute("UPDATE wf_events SET status='pending', "
                 "attempts=CASE WHEN attempts > 0 THEN attempts-1 ELSE 0 END, error=?, updated_at=?, not_before=? "
                 "WHERE event_id=?", (reason[:2000], now(), _later(seconds), event_id))
    conn.commit()
    _release_for_event(conn, event_id)


def recover_running(conn):
    """On worker start: anything left 'running' died with the previous process.

    An event that has already used its attempts is parked as failed, otherwise a handler that kills
    the process (a crash inside a native library on a bad recording) would be re-claimed first on
    every start and nothing else would ever run. A recovered event waits its backoff like a failed one.

    Postgres runs several worker processes: an event whose owner lock another session still holds is
    being handled right now, not dead, and is left alone (the lock dies with the session that held it,
    so a crashed worker's events are free to recover)."""
    rows = conn.execute("SELECT id, attempts, owner FROM wf_events WHERE status='running'").fetchall()
    for row in rows:
        if conn.dialect == db.POSTGRES:
            key = _owner_key(row["owner"])
            ours = key in _held(conn)
            if not ours and not _try_owner_lock(conn, key):
                continue                 # alive in another worker
        failed = row["attempts"] >= MAX_ATTEMPTS
        conn.execute("UPDATE wf_events SET status=?, error=COALESCE(error, 'the worker died while handling this event'), "
                     "updated_at=?, not_before=? WHERE id=? AND status='running'",
                     ("failed" if failed else "pending", now(), _later(row["attempts"] * RETRY_BACKOFF_S), row["id"]))
        conn.commit()
        if conn.dialect == db.POSTGRES and not ours:
            _release_owner_lock(conn, key)
    conn.commit()


def pending(conn):
    return conn.execute(
        "SELECT * FROM wf_events WHERE status IN ('pending','running','failed') ORDER BY id").fetchall()


def queue_depth(conn) -> list:
    """Per owner: how many events are pending, running and failed, and the oldest pending one's age
    marker (its created_at). For `salescoach status` and the ops panel."""
    rows = conn.execute(
        "SELECT owner, "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending, "
        "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
        "MIN(CASE WHEN status='pending' THEN created_at END) AS oldest_pending "
        "FROM wf_events WHERE status IN ('pending','running','failed') GROUP BY owner ORDER BY owner").fetchall()
    return [{"owner": r["owner"], "pending": int(r["pending"] or 0), "running": int(r["running"] or 0),
             "failed": int(r["failed"] or 0), "oldest_pending": r["oldest_pending"]} for r in rows]
