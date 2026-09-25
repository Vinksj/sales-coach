"""Phase 6: process roles (salescoach/ops.py, `serve --role`).

web serves HTTP and starts no worker; worker runs N loops and serves nothing; scheduler runs the
duties under a leader election; every worker/scheduler writes a heartbeat that /health shows.
"""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from salescoach import cli, ops
from salescoach.orchestrator import bus, workflow
from salescoach.schemas.events import Event
from salescoach.store import stores
from salescoach.web.app import create_app


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(ops, "HEARTBEAT_S", 0.2)
    monkeypatch.setattr(ops, "LEADER_RETRY_S", 0.2)


def _publish(conn, owner, n=1):
    for i in range(n):
        assert bus.publish(conn, Event(type="T", entity_id=f"user:{owner}", dedupe_key=f"T:{owner}:{i}:{time.monotonic_ns()}"))
    conn.commit()


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ---- web -----------------------------------------------------------------------------------------------

def test_web_role_serves_http_and_starts_no_worker(db):
    with TestClient(create_app(role="web", live_factory=None, hub=None)) as c:      # start_worker left at True
        assert c.app.state.worker is None and c.app.state.worker_thread is None
        from salescoach.orchestrator.worker import Worker
        assert not [t for t in threading.enumerate() if isinstance(t, Worker)]
        body = c.get("/health").json()
        assert body["role"] == "web" and body["worker"] == "off" and body["processes"]["workers"] == []
        assert c.get("/").status_code == 200


def test_create_app_refuses_a_non_http_role(db):
    with pytest.raises(ValueError):
        create_app(role="worker")


def test_all_role_writes_the_heartbeats_the_split_roles_write(db):
    with TestClient(create_app(role="all", heartbeats=True, live_factory=None, hub=None)) as c:
        assert c.app.state.worker_thread.is_alive()
        assert _wait(lambda: len(ops.read_heartbeats(stores.sales())["workers"]) == 1)
        body = c.get("/health").json()
        assert body["role"] == "all" and body["worker"] == "running"
        beats = body["processes"]
        assert beats["workers"][0]["host"] == ops.hostname() and beats["workers"][0]["concurrency"] == 1
        assert not beats["workers"][0]["stale"]
        assert _wait(lambda: ops.read_heartbeats(stores.sales())["leader"] is not None)
        assert beats["leader"] is None or beats["leader"]["host"] == ops.hostname()


def test_web_role_shows_the_worker_as_on_when_a_worker_process_beats(db):
    with TestClient(create_app(role="web", live_factory=None, hub=None)) as c:
        assert "worker off" in c.get("/").text
        ops.write_heartbeat(db, "worker", concurrency=2, handled=0)
        assert "worker off" not in c.get("/").text
        old = json.loads(db.execute("SELECT value FROM state WHERE key=?", (ops.heartbeat_key("worker"),)).fetchone()[0])
        old["at"] = "2000-01-01T00:00:00+00:00"
        stores.set_state(db, ops.heartbeat_key("worker"), json.dumps(old))
        db.commit()
        assert "worker off" in c.get("/").text                         # a stale heartbeat is a dead worker
        assert c.get("/health").json()["processes"]["workers"][0]["stale"] is True


# ---- the CLI -------------------------------------------------------------------------------------------

def test_serve_role_worker_and_scheduler_never_serve_http(monkeypatch, db):
    import uvicorn
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.append(("uvicorn", kw)))
    monkeypatch.setattr("salescoach.web.app.create_app", lambda *a, **k: ran.append(("app", k)) or object())
    monkeypatch.setattr(ops, "run_worker", lambda **kw: ran.append(("worker", kw)))
    monkeypatch.setattr(ops, "run_scheduler", lambda **kw: ran.append(("scheduler", kw)))
    assert cli.main(["serve", "--role", "worker", "--host", "0.0.0.0", "--concurrency", "3"]) == 0   # no password needed
    assert ran == [("worker", {"n": 3})]
    ran.clear()
    assert cli.main(["serve", "--role", "scheduler"]) == 0 and ran == [("scheduler", {})]
    ran.clear()
    monkeypatch.setenv("SALESCOACH_ROLE", "web")
    cli.main(["serve"])
    assert [r[0] for r in ran] == ["app", "uvicorn"] and ran[0][1]["role"] == "web" and ran[0][1]["heartbeats"] is False
    ran.clear()
    monkeypatch.delenv("SALESCOACH_ROLE")
    cli.main(["serve"])
    assert ran[0][1] == {"role": "all", "heartbeats": True}


def test_health_command_reads_the_heartbeat_for_a_worker(db, capsys):
    assert cli.main(["health", "--role", "worker"]) == 1
    assert "no worker heartbeat" in capsys.readouterr().err
    ops.write_heartbeat(db, "worker", concurrency=2)
    assert cli.main(["health", "--role", "worker"]) == 0
    assert "ok" in capsys.readouterr().out
    assert cli.main(["health", "--role", "scheduler"]) == 1
    assert cli.main(["health", "--role", "web", "--port", "1"]) == 1     # nothing listens there


# ---- worker ---------------------------------------------------------------------------------------------

def test_run_worker_runs_n_loops_and_beats(db, fast, monkeypatch, dialect, bus_rows):
    seen, lock = [], threading.Lock()

    def handler(conn, ev):
        with lock:
            seen.append(threading.current_thread().name)
        time.sleep(0.2)
    monkeypatch.setitem(workflow.HANDLERS, "T", [handler])
    if dialect == "postgres":                                 # two users: two owners can run at once
        from salescoach import users
        owners = [users.create(db, f"{n}@tessel.test", n)["id"] for n in ("Asha", "Bala")]
        db.commit()
    else:
        owners = ["local"]                                    # SQLite holds one user; the loops serialise on it
    for i in range(4):
        _publish(bus_rows, owners[i % len(owners)])
    stop = threading.Event()
    handle = ops.run_worker(n=2, stop=stop, block=False)
    try:
        assert len(handle["workers"]) == 2
        assert _wait(lambda: len(seen) == 4)
        assert {n.rsplit("-", 1)[0] for n in seen} == {"salescoach-worker"}
        if dialect == "postgres":
            assert len(set(seen)) == 2                        # both loops worked
        assert _wait(lambda: ops.read_heartbeats(stores.sales())["workers"] != [])
        beat = ops.read_heartbeats(stores.sales())["workers"][0]
        assert beat["concurrency"] == 2 and beat["pid"] and not beat["stale"]
        assert _wait(lambda: ops.read_heartbeats(stores.sales())["workers"][0]["handled"] == 4)
    finally:
        stop.set()
    for w in handle["workers"]:
        w.join(timeout=5)
        assert not w.is_alive()
    handle["heartbeat"].join(timeout=5)
    assert bus_rows.execute("SELECT COUNT(*) FROM wf_events WHERE status='done'").fetchone()[0] == 4


# ---- scheduler and the leader election -------------------------------------------------------------------

def test_leader_lock_is_exclusive_and_passes_on_release(db, dialect):
    a, b = ops.Leader(), ops.Leader()
    try:
        assert a.try_acquire() and a.held and a.alive()
        if dialect == "sqlite":
            assert b.try_acquire()                              # one process assumed: no lock to take
            return
        assert not b.try_acquire() and not b.held
        assert not b.try_acquire()
        a.release()
        assert not a.held
        assert b.try_acquire() and b.held
        assert not a.try_acquire()
    finally:
        a.release()
        b.release()


def test_two_schedulers_elect_one_leader_and_the_other_takes_over(db, dialect, fast):
    starts, stops = [], []

    def duties(tag):
        def start(db_path, stop):
            starts.append(tag)

            def watch():
                stop.wait()
                stops.append(tag)
            threading.Thread(target=watch, daemon=True).start()
        return start

    stop_a, stop_b = threading.Event(), threading.Event()
    a = ops.run_scheduler(stop=stop_a, start_duties=duties("a"), block=False)
    assert _wait(lambda: starts == ["a"])
    leader = ops.read_heartbeats(stores.sales())["leader"]
    assert _wait(lambda: ops.read_heartbeats(stores.sales())["leader"] is not None)
    leader = ops.read_heartbeats(stores.sales())["leader"]
    assert leader["host"] == ops.hostname() and not leader["stale"]
    b = ops.run_scheduler(stop=stop_b, start_duties=duties("b"), block=False)
    try:
        if dialect == "sqlite":
            assert _wait(lambda: starts == ["a", "b"])       # no election on SQLite: documented single process
            return
        time.sleep(1.0)
        assert starts == ["a"] and a["leader"] and not b["leader"]
        assert ops.read_heartbeats(stores.sales())["leader"]["host"] == ops.hostname()
        stop_a.set()                                          # the leader goes away (a deploy, a crash)
        assert _wait(lambda: stops == ["a"])
        assert _wait(lambda: starts == ["a", "b"], timeout=15)
        assert b["leader"] and b["terms"] == 1
    finally:
        stop_a.set()
        stop_b.set()
        a["thread"].join(timeout=5)
        b["thread"].join(timeout=5)


@pytest.mark.postgres_only
def test_a_leader_that_loses_its_session_stands_down(db, fast):
    starts, stops = [], []

    def start(db_path, stop):
        starts.append(1)
        threading.Thread(target=lambda: (stop.wait(), stops.append(1)), daemon=True).start()

    stop = threading.Event()
    state = ops.run_scheduler(stop=stop, start_duties=start, block=False)
    try:
        assert _wait(lambda: starts == [1])
        state["election"].conn.close()                        # the lock session dies under it
        assert _wait(lambda: stops == [1])                    # duties told to stop
        assert _wait(lambda: starts == [1, 1], timeout=15)    # ... and it stands again (nobody else wanted it)
        assert state["terms"] == 2
    finally:
        stop.set()
        state["thread"].join(timeout=5)


def test_heartbeat_thread_writes_and_stale_is_by_age(db, fast):
    stop = threading.Event()
    beat = ops.Heartbeat(None, "scheduler", stop, fields=lambda: {"leader": False}, interval_s=0.1)
    beat.start()
    assert _wait(lambda: beat.beats >= 2)
    stop.set()
    beat.join(timeout=5)
    mine = ops.read_heartbeats(db)["schedulers"]
    assert len(mine) == 1 and mine[0]["leader"] is False and mine[0]["age_s"] < ops.STALE_AFTER_S and not mine[0]["stale"]
    assert ops.check_health("scheduler") == (True, f"scheduler heartbeat {mine[0]['age_s']}s old") or ops.check_health("scheduler")[0]
