"""Process roles, heartbeats and the scheduler's leader election (Phase 6).

One image, three roles (`salescoach serve --role`, or SALESCOACH_ROLE):

  all        the local install and the single-container deploy: HTTP, the worker, the scheduler's
             duties and the embed/learning loops in one process, exactly as before.
  web        HTTP only. No worker thread, no duties, no Jarvis sync, no embed loop. Scale it out freely.
  worker     WORKER_CONCURRENCY worker loops (each its own connection, each claiming from the bus with
             the per-owner fairness rule) and nothing else. Scale it out freely.
  scheduler  the duties (follow-ups, replies, calendar, autosend, the sources poller, the learning
             recompute) and the embed loop, under a leader election: on Postgres every scheduler
             process tries a session advisory lock and only the holder runs anything; the others wait
             and take over when the holder's session ends (a crash, a deploy). On SQLite there is one
             process by construction (one file, one writer) and the election is a no-op.

Every worker and scheduler process writes a heartbeat into the org-wide `state` table:
  ops:worker:<hostname>:heartbeat      {"at", "pid", "started_at", "concurrency", "handled"}
  ops:scheduler:<hostname>:heartbeat   {"at", "pid", "started_at", "leader": bool}
  ops:scheduler:leader                 {"host", "pid", "since", "at"}   (the holder refreshes it)
/health on a web process reports them with their age, so a dead process is visible from outside;
`salescoach health` (the container health check) reads the same rows for a worker or a scheduler,
which serve no HTTP.
"""
import json
import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from . import identity
from .store import db, stores
from .store.stores import now, set_state

log = logging.getLogger("salescoach.ops")

ROLE_ENV = "SALESCOACH_ROLE"
ROLES = ("all", "web", "worker", "scheduler")
CONCURRENCY_ENV = "WORKER_CONCURRENCY"
HEARTBEAT_S = 15.0                     # how often a process says it is alive
STALE_AFTER_S = 3 * HEARTBEAT_S + 5    # a heartbeat older than this is a dead (or wedged) process
LEADER_RETRY_S = 5.0                   # a standby scheduler tries for the lock this often
LEADER_KEY = "salescoach:scheduler"    # hashed with current_schema() so test schemas never share a lock


def role_from_env() -> str:
    value = (os.environ.get(ROLE_ENV) or "all").strip().lower()
    return value if value in ROLES else "all"


def concurrency() -> int:
    try:
        return max(1, int(os.environ.get(CONCURRENCY_ENV) or 2))
    except ValueError:
        return 2


def hostname() -> str:
    return socket.gethostname().split(".")[0] or "host"


def _age_s(stamp: Optional[str]) -> Optional[float]:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())


# ---- heartbeats ---------------------------------------------------------------------------------------

def heartbeat_key(kind: str, host: Optional[str] = None) -> str:
    return f"ops:{kind}:{host or hostname()}:heartbeat"


def write_heartbeat(conn, kind: str, **fields) -> None:
    """One row per process kind and host, org-wide (`state` is SYSTEM: no actor needed)."""
    body = {"at": now(), "pid": os.getpid(), "host": hostname(), **fields}
    set_state(conn, heartbeat_key(kind), json.dumps(body))
    conn.commit()


def read_heartbeats(conn) -> dict:
    """{"workers": [...], "schedulers": [...], "leader": {...}|None}, each entry with age_s and stale."""
    out = {"workers": [], "schedulers": [], "leader": None}
    rows = conn.execute("SELECT key, value FROM state WHERE key LIKE 'ops:%'").fetchall()
    for row in rows:
        try:
            body = json.loads(row["value"] or "{}")
        except ValueError:
            continue
        age = _age_s(body.get("at"))
        body = {**body, "age_s": None if age is None else round(age, 1), "stale": age is None or age > STALE_AFTER_S}
        key = row["key"]
        if key == "ops:scheduler:leader":
            out["leader"] = body
        elif key.startswith("ops:worker:") and key.endswith(":heartbeat"):
            out["workers"].append(body)
        elif key.startswith("ops:scheduler:") and key.endswith(":heartbeat"):
            out["schedulers"].append(body)
    for kind in ("workers", "schedulers"):
        out[kind].sort(key=lambda b: str(b.get("host")))
    return out


def _open(db_path):
    with identity.activate(None):          # the heartbeat is nobody's: state is org-wide
        return stores.sales(db_path)


class Heartbeat(threading.Thread):
    """Writes a process's heartbeat every HEARTBEAT_S until `stop` is set. `fields()` adds the live
    numbers (events handled, leader or not)."""

    def __init__(self, db_path, kind: str, stop: threading.Event, fields: Optional[Callable] = None,
                 interval_s: Optional[float] = None):
        super().__init__(name=f"salescoach-heartbeat-{kind}", daemon=True)
        self.db_path, self.kind, self.stop, self.fields = db_path, kind, stop, fields
        self.interval_s = interval_s                 # None: HEARTBEAT_S as it is at each beat
        self.started_at = now()
        self.beats = 0

    def beat(self, conn) -> None:
        extra = self.fields() if self.fields else {}
        write_heartbeat(conn, self.kind, started_at=self.started_at, **extra)
        self.beats += 1

    def run(self):
        conn = None
        while True:
            try:
                if conn is None:
                    conn = _open(self.db_path)
                self.beat(conn)
            except Exception:
                log.exception("%s heartbeat failed", self.kind)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
            if self.stop.wait(self.interval_s or HEARTBEAT_S):
                break
        if conn is not None:
            conn.close()


# ---- the worker role -------------------------------------------------------------------------------

def start_workers(db_path, n: int, stop: threading.Event, live_busy=lambda: False) -> list:
    """`n` worker loops, each on its own connection (its own bus session, so its own owner locks)."""
    from .orchestrator.worker import Worker
    workers = []
    for i in range(max(1, n)):
        w = Worker(live_busy=live_busy, db_path=db_path)
        w.name = f"salescoach-worker-{i + 1}"
        w.start()
        workers.append(w)

    def stopper():
        stop.wait()
        for w in workers:
            w.stop()
    threading.Thread(target=stopper, name="salescoach-worker-stopper", daemon=True).start()
    return workers


def run_worker(db_path=None, n: Optional[int] = None, stop: Optional[threading.Event] = None, block: bool = True) -> dict:
    """The `worker` role: N loops plus a heartbeat, until SIGTERM/SIGINT (or `stop`)."""
    stop = stop or threading.Event()
    n = n or concurrency()
    workers = start_workers(db_path, n, stop)
    beat = Heartbeat(db_path, "worker", stop,
                     fields=lambda: {"concurrency": n, "handled": sum(w.handled for w in workers),
                                     "busy": sum(1 for w in workers if w.current is not None)})
    beat.start()
    log.info("worker: %d loops on %s", n, hostname())
    handle = {"workers": workers, "heartbeat": beat, "stop": stop}
    if block:
        _wait_for_signal(stop)
        for w in workers:
            w.join(timeout=10)
    return handle


# ---- the scheduler role and its leader election ------------------------------------------------------

class Leader:
    """The scheduler lock. Postgres: a session advisory lock on a connection of our own (not pooled: the
    lock must live exactly as long as this object holds it), keyed on the schema so two installs, or two
    test schemas, in one cluster never share it. SQLite: always ours (one process per file)."""

    def __init__(self, db_path=None):
        self.db_path = db_path
        self.target = db_path if db_path is not None else stores.db_path()
        self.postgres = isinstance(self.target, str) and db.is_postgres_url(self.target)
        self.conn = None
        self.held = False
        self.since = None

    def _connect(self):
        conn = db.connect(self.target)
        schema = stores._pg_schema
        if schema:
            conn.execute(f'SET search_path TO "{schema}"')
        return conn

    def try_acquire(self) -> bool:
        if self.held:
            return True
        if not self.postgres:
            self.held, self.since = True, now()
            return True
        try:
            if self.conn is None:
                self.conn = self._connect()
            got = self.conn.execute("SELECT pg_try_advisory_lock(hashtext(? || ':' || current_schema()))",
                                    (LEADER_KEY,)).fetchone()[0]
        except Exception:
            log.exception("scheduler: the lock connection failed; reconnecting")
            self._drop()
            return False
        if got:
            self.held, self.since = True, now()
        return bool(got)

    def alive(self) -> bool:
        """Still the holder? The lock lives with the session: if the session is gone, so is the lock."""
        if not self.held:
            return False
        if not self.postgres:
            return True
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            log.exception("scheduler: lost the lock connection; standing down")
            self._drop()
            return False

    def release(self) -> None:
        if self.postgres and self.conn is not None:
            try:
                self.conn.execute("SELECT pg_advisory_unlock_all()")
            except Exception:
                pass
        self._drop()

    def _drop(self):
        self.held, self.since = False, None
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def _default_duties(db_path, stop):
    from . import plugins
    plugins.start_background(db_path, stop)


def run_scheduler(db_path=None, stop: Optional[threading.Event] = None, start_duties: Callable = _default_duties,
                  block: bool = True, on_change: Optional[Callable] = None) -> dict:
    """The `scheduler` role. Loop: try the lock; as leader, start the duties and refresh the leader row
    every HEARTBEAT_S while the lock connection answers; standing by, say so every LEADER_RETRY_S. When
    the lock is lost the duties' stop is set (a duty in flight finishes its round, then its thread
    ends) and the loop goes back to trying. Always a heartbeat, leader or not."""
    stop = stop or threading.Event()
    leader = Leader(db_path)
    state = {"leader": False, "terms": 0, "stop": stop}
    beat = Heartbeat(db_path, "scheduler", stop, fields=lambda: {"leader": state["leader"]})
    beat.start()

    def loop():
        duties_stop = None
        while not stop.is_set():
            if leader.try_acquire():
                state["leader"], state["terms"] = True, state["terms"] + 1
                duties_stop = threading.Event()
                try:
                    conn = _open(db_path)
                    try:
                        _write_leader(conn, leader.since)
                    finally:
                        conn.close()
                    start_duties(db_path, duties_stop)
                except Exception:
                    log.exception("scheduler: could not start the duties; releasing the lock")
                    duties_stop.set()
                    state["leader"] = False
                    leader.release()
                    stop.wait(LEADER_RETRY_S)
                    continue
                if on_change:
                    on_change(True)
                log.info("scheduler: leader on %s", hostname())
                while not stop.wait(HEARTBEAT_S):
                    if not leader.alive():
                        break
                    try:
                        conn = _open(db_path)
                        try:
                            _write_leader(conn, leader.since)
                        finally:
                            conn.close()
                    except Exception:
                        log.exception("scheduler: leader heartbeat failed")
                duties_stop.set()
                state["leader"] = False
                leader.release()
                if on_change:
                    on_change(False)
                log.info("scheduler: standing down on %s", hostname())
            else:
                stop.wait(LEADER_RETRY_S)
        leader.release()

    thread = threading.Thread(target=loop, name="salescoach-scheduler-leader", daemon=True)
    thread.start()
    state["thread"], state["heartbeat"], state["election"] = thread, beat, leader
    if block:
        _wait_for_signal(stop)
        thread.join(timeout=10)
    return state


def _write_leader(conn, since) -> None:
    set_state(conn, "ops:scheduler:leader", json.dumps({"host": hostname(), "pid": os.getpid(), "since": since,
                                                         "at": now()}))
    conn.commit()


def _wait_for_signal(stop: threading.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:                   # not the main thread (tests)
            break
    while not stop.wait(1.0):
        pass
    time.sleep(0.05)


# ---- the container health check ------------------------------------------------------------------------

def check_health(role: str, db_path=None, port: Optional[int] = None) -> tuple[bool, str]:
    """`salescoach health`: a web/all process answers over HTTP; a worker or scheduler has no HTTP, so its
    own heartbeat row (this hostname) must be fresh."""
    if role in ("web", "all"):
        import urllib.request
        url = f"http://127.0.0.1:{port or int(os.environ.get('PORT') or 8140)}/health"
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:
                return resp.status == 200, f"HTTP {resp.status}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    conn = _open(db_path)
    try:
        beats = read_heartbeats(conn)
    finally:
        conn.close()
    mine = [b for b in beats["workers" if role == "worker" else "schedulers"] if b.get("host") == hostname()]
    if not mine:
        return False, f"no {role} heartbeat for {hostname()} yet"
    b = mine[0]
    return not b["stale"], f"{role} heartbeat {b['age_s']}s old" + (" (stale)" if b["stale"] else "")
