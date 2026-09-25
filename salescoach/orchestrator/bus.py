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
    (pg_try_advisory_lock, keyed on schema and owner) BEFORE marking the row running and holds it until the
    event is settled (complete / fail / defer). Advisory locks are atomic across
    sessions, so two claimers racing for one owner's events cannot both win; the
    row itself is taken with an UPDATE ... WHERE status='pending' whose rowcount
    says whether we got it. No FOR UPDATE is needed for that.
  * SQLite: one process (docs/deploy-cloud.md); the claim runs under the write
    lock (BEGIN IMMEDIATE), which serialises claimers, so a NOT EXISTS on running
    events of the same owner is race-free there.

Who may touch which event (Postgres, store/rls.py). A user session, interactive or
service, reads and writes only its own events, and publishes only an event whose
entity names the actor as owner. The machinery (claim_next, complete, fail, defer,
recover_running) runs with NOBODY bound: _machinery() unbinds the connection for
the call, and the app_bus_* SECURITY DEFINER functions are the only way to list,
take or settle another owner's event. They refuse a session with an actor bound,
and take or settle an event only while this session holds its owner's lock.
"""
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import identity
from ..schemas.events import Event
from ..store import db
from ..store.stores import now

log = logging.getLogger("salescoach.bus")

MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 30      # a failed event waits attempts x this before it is claimed again
CLAIM_SCAN = 50           # Postgres: how many owners' best claimable events one claim looks at before giving up

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


@contextmanager
def _machinery(conn):
    """Postgres: run the block with nobody bound (the app_bus_* functions refuse any session with an actor),
    then give the connection its actor back. A worker's connection is unbound already; the CLI's `work` and
    the tests drain on a bound one. SQLite has no row-level security: nothing to do."""
    if conn.dialect != db.POSTGRES:
        yield conn
        return
    actor = conn.actor
    if actor is not None:
        identity.bind(conn, None)
    try:
        with conn.as_system():
            yield conn
    finally:
        if actor is not None:
            identity.bind(conn, actor)


def claim_next(conn) -> Optional[Event]:
    """Atomically move the best claimable pending event (highest priority, then oldest, whose owner has
    nothing running) to running and return it."""
    if conn.in_transaction:
        # Nothing legitimate is pending here: handlers commit their own work and a failed handler's
        # partial writes must not ride along with the next claim.
        conn.rollback()
    try:
        if conn.dialect == db.POSTGRES:
            with _machinery(conn):
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

def _owner_key(conn, owner) -> str:
    """The advisory-lock key of an owner's events. Advisory locks are database-wide, so the key names the schema
    too: two installs (or two test sessions) in one database never hold each other's owners. app_bus_owner_locked
    (store/rls.py) computes the same key."""
    schema = getattr(conn, "_bus_schema", None)
    if schema is None:
        schema = conn._bus_schema = conn.execute("SELECT current_schema()").fetchone()[0]
    return f"salescoach:owner:{schema}:{owner or ''}"


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


def _peek(conn, event_id: str):
    """(owner, attempts, status) of an event, whoever owns it; None when there is none. Postgres: inside
    _machinery() only (app_bus_peek refuses a bound session)."""
    if conn.dialect == db.POSTGRES:
        row = conn.execute("SELECT ev_owner AS owner, ev_attempts AS attempts, ev_status AS status "
                           "FROM app_bus_peek(?)", (event_id,)).fetchone()
    else:
        row = conn.execute("SELECT owner, attempts, status FROM wf_events WHERE event_id=?", (event_id,)).fetchone()
    return None if row is None else (row["owner"], row["attempts"], row["status"])


def stored_owner(conn, event_id: str) -> Optional[str]:
    """The owner bus.publish recorded for an event. With nobody bound (the worker, before it binds the owner)
    through the machinery; a bound session reads its own events only, so it learns only its own."""
    if conn.dialect == db.POSTGRES and conn.actor is not None:
        row = conn.execute("SELECT owner FROM wf_events WHERE event_id=?", (event_id,)).fetchone()
        return row["owner"] if row is not None else None
    with _machinery(conn):
        found = _peek(conn, event_id)
    return found[0] if found else None


def _release_for_event(conn, event_id: str) -> None:
    if conn.dialect != db.POSTGRES or not _held(conn):
        return
    found = _peek(conn, event_id)
    _release_owner_lock(conn, _owner_key(conn, found[0] if found else None))


def _claim_postgres(conn) -> Optional[Event]:
    # One candidate per owner (that owner's best), then the best of those first. Listing the top CLAIM_SCAN
    # events instead let one owner with CLAIM_SCAN or more events ahead of everyone else, one of them running
    # under that owner's lock, fill the whole scan: every free worker found only locked candidates and other
    # reps' events waited for the backlog to drain, the starvation the per-owner lock exists to prevent.
    # The listing and the take are app_bus_candidates / app_bus_take (store/rls.py): the caller is nobody, and the
    # take refuses an event whose owner's lock this session does not hold.
    candidates = conn.execute("SELECT ev_id AS id, ev_owner AS owner FROM app_bus_candidates(?::integer, ?, ?::integer)",
                              (MAX_ATTEMPTS, now(), CLAIM_SCAN)).fetchall()
    busy = set()
    for cand in candidates:
        key = _owner_key(conn, cand["owner"])
        if key in busy:
            continue
        if not _try_owner_lock(conn, key):
            busy.add(key)                # another session is handling this owner's event: theirs come later
            continue
        row = conn.execute("SELECT * FROM app_bus_take(?::integer, ?)", (cand["id"], now())).fetchone()
        if row is None:                  # settled or re-timed by someone since we listed it
            conn.rollback()
            _release_owner_lock(conn, key)
            continue
        conn.commit()
        return _to_event(row)
    return None


# ---- settling ---------------------------------------------------------------------------------------

def _settle(conn, event_id, status, error, not_before, refund=False, keep_error=False, only_running=False):
    """One event to done / failed / pending. Postgres: app_bus_settle, inside _machinery(); a running event only
    under its owner's lock (store/rls.py). SQLite: the same UPDATE by hand."""
    if conn.dialect == db.POSTGRES:
        conn.execute("SELECT app_bus_settle(?, ?, ?, ?::integer = 1, ?::integer = 1, ?, ?, ?::integer = 1)",  # bools as 0/1
                     (event_id, status, error, int(keep_error), int(refund), not_before, now(), int(only_running)))
        return
    sets = ["status=?", "error=COALESCE(error, ?)" if keep_error else "error=?", "updated_at=?"]
    params = [status, error, now()]
    if status != "done":
        sets.append("not_before=?")
        params.append(not_before)
    if refund:
        sets.append("attempts=CASE WHEN attempts > 0 THEN attempts-1 ELSE 0 END")
    conn.execute(f"UPDATE wf_events SET {', '.join(sets)} WHERE event_id=?" + (" AND status='running'" if only_running else ""),
                 (*params, event_id))


def complete(conn, event_id: str):
    with _machinery(conn):
        _settle(conn, event_id, "done", None, None)
        conn.commit()
        _release_for_event(conn, event_id)


def fail(conn, event_id: str, error: str):
    """Return the event to pending until it has used MAX_ATTEMPTS, then park it as failed."""
    with _machinery(conn):
        found = _peek(conn, event_id)
        attempts = found[1] if found is not None else MAX_ATTEMPTS
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        _settle(conn, event_id, status, error[:2000], _later(attempts * RETRY_BACKOFF_S))
        conn.commit()
        _release_for_event(conn, event_id)
    return status


def defer(conn, event_id: str, seconds: int, reason: str):
    """Put an event back without spending an attempt (e.g. the model quota is exhausted)."""
    with _machinery(conn):
        _settle(conn, event_id, "pending", reason[:2000], _later(seconds), refund=True)
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
    with _machinery(conn):
        if conn.dialect == db.POSTGRES:
            rows = conn.execute("SELECT ev_event_id AS event_id, ev_attempts AS attempts, ev_owner AS owner "
                                "FROM app_bus_running()").fetchall()
        else:
            rows = conn.execute("SELECT event_id, attempts, owner FROM wf_events WHERE status='running' "
                                "ORDER BY id").fetchall()
        for row in rows:
            if conn.dialect == db.POSTGRES:
                key = _owner_key(conn, row["owner"])
                ours = key in _held(conn)
                if not ours and not _try_owner_lock(conn, key):
                    continue                 # alive in another worker
            failed = row["attempts"] >= MAX_ATTEMPTS
            _settle(conn, row["event_id"], "failed" if failed else "pending", "the worker died while handling this event",
                    _later(row["attempts"] * RETRY_BACKOFF_S), keep_error=True, only_running=True)
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
    if conn.dialect == db.POSTGRES:
        # app_bus_depth: numbers per owner, for the machinery or an active admin (a rep reads only their own events)
        rows = conn.execute("SELECT ev_owner AS owner, ev_pending AS pending, ev_running AS running, ev_failed AS failed, "
                            "ev_oldest_pending AS oldest_pending FROM app_bus_depth()").fetchall()
        return [{"owner": r["owner"], "pending": int(r["pending"] or 0), "running": int(r["running"] or 0),
                 "failed": int(r["failed"] or 0), "oldest_pending": r["oldest_pending"]} for r in rows]
    rows = conn.execute(
        "SELECT owner, "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending, "
        "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
        "MIN(CASE WHEN status='pending' THEN created_at END) AS oldest_pending "
        "FROM wf_events WHERE status IN ('pending','running','failed') GROUP BY owner ORDER BY owner").fetchall()
    return [{"owner": r["owner"], "pending": int(r["pending"] or 0), "running": int(r["running"] or 0),
             "failed": int(r["failed"] or 0), "oldest_pending": r["oldest_pending"]} for r in rows]
