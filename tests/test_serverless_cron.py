"""Serverless: the worker and the scheduler as cron calls (salescoach/cron.py).

  * drain(): handles the queue with the worker's own claim / handle / settle loop, stops CLAIMING when its budget is
    spent (what it did not claim stays pending, nothing is left running), and records itself (ops:cron:drain),
    which is what /health and the "worker" pill read on a serverless deployment;
  * two drains that overlap (a slow event outlasting the minute) never run one event twice;
  * tick(): runs each duty that is due, once, then not again until the delay its round returned; a duty past the
    budget waits for the next tick; a tick while another tick (or a scheduler process) holds the scheduler lock
    runs nothing (Postgres: the lock is real there; SQLite has one process by construction).
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from salescoach import cron, ops
from salescoach.automation.scheduler import Duty
from salescoach.orchestrator import bus, workflow
from salescoach.schemas.events import Event
from salescoach.store import stores


def _publish(conn, n, kind="T"):
    for i in range(n):
        assert bus.publish(conn, Event(type=kind, entity_id=f"e{i}", dedupe_key=f"{kind}:{i}"))
    conn.commit()


@pytest.fixture
def handled(monkeypatch):
    """A handler for event type T that records what it ran (and can be slowed down)."""
    seen = {"ids": [], "delay": 0.0}
    lock = threading.Lock()

    def handler(conn, event):
        time.sleep(seen["delay"])
        with lock:
            seen["ids"].append(event.event_id)
    monkeypatch.setitem(workflow.HANDLERS, "T", [handler])
    return seen


def _statuses(conn):
    return dict(conn.execute("SELECT status, COUNT(*) FROM wf_events GROUP BY status").fetchall())


def test_a_drain_handles_the_queue_and_records_itself(db, bus_rows, handled):
    _publish(bus_rows, 5)
    result = cron.drain(budget_s=30)
    assert result["handled"] == 5 and result["stopped"] == "empty"
    assert len(handled["ids"]) == 5 and _statuses(bus_rows) == {"done": 5}
    beat = ops.read_heartbeats(db)["cron"]["drain"]
    assert beat["handled"] == 5 and not beat["stale"]
    assert ops.cron_worker_live(db)


def test_a_drain_stops_claiming_at_its_budget_and_leaves_the_rest_pending(db, bus_rows, handled):
    handled["delay"] = 0.3
    _publish(bus_rows, 10)
    result = cron.drain(budget_s=0.5)
    assert result["stopped"] == "budget" and 1 <= result["handled"] < 10, result
    counts = _statuses(bus_rows)
    assert counts.get("running", 0) == 0                                  # nothing claimed and abandoned
    assert counts["done"] == result["handled"] and counts["pending"] == 10 - result["handled"]
    handled["delay"] = 0.0
    assert cron.drain(budget_s=30)["handled"] == 10 - result["handled"]   # the next call picks the rest up
    assert len(handled["ids"]) == len(set(handled["ids"])) == 10


def test_a_stale_drain_says_the_worker_is_off(db, monkeypatch):
    assert not ops.cron_worker_live(db)                                   # never drained
    cron._record(None, cron.DRAIN_KEY, {"handled": 0})
    assert ops.cron_worker_live(db)
    monkeypatch.setenv(ops.CRON_STALE_ENV, "1")
    time.sleep(1.2)
    assert not ops.cron_worker_live(db)


def test_two_overlapping_drains_never_run_an_event_twice(db, bus_rows, handled):
    handled["delay"] = 0.02
    _publish(bus_rows, 30)
    results, errors = [], []
    barrier = threading.Barrier(2)

    def one():
        try:
            barrier.wait(timeout=10)
            results.append(cron.drain(budget_s=20))
        except Exception as exc:                        # pragma: no cover - reported below
            errors.append(repr(exc))
    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert sum(r["handled"] for r in results) == 30
    assert len(handled["ids"]) == 30 and len(set(handled["ids"])) == 30
    assert _statuses(bus_rows) == {"done": 30}


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def test_a_tick_runs_a_due_duty_once_then_waits_for_its_interval(db):
    ran = []
    duty = Duty("probe", lambda conn: ran.append(1) or {"ok": True}, lambda: 600.0, first_delay_s=0)
    clock = Clock()
    first = cron.tick(duties=[duty], clock=clock)
    assert first["ran"] == ["probe"] and len(ran) == 1
    clock.t += timedelta(seconds=599)
    assert cron.tick(duties=[duty], clock=clock)["not_due"] == ["probe"] and len(ran) == 1
    clock.t += timedelta(seconds=2)
    assert cron.tick(duties=[duty], clock=clock)["ran"] == ["probe"] and len(ran) == 2
    assert ops.read_heartbeats(db)["cron"]["tick"]["ran"] == ["probe"]
    assert db.execute("SELECT value FROM state WHERE key='ops:scheduler:leader'").fetchone() is not None


def test_a_tick_past_its_budget_leaves_the_rest_due(db):
    ran = []
    slow = Duty("slow", lambda conn: time.sleep(0.3) or ran.append("slow"), lambda: 600.0)
    later = Duty("later", lambda conn: ran.append("later"), lambda: 600.0)
    result = cron.tick(duties=[slow, later], budget_s=0.2)
    assert result["ran"] == ["slow"] and result["deferred"] == ["later"] and ran == ["slow"]
    assert cron.tick(duties=[slow, later], budget_s=0.2)["ran"] == ["later"]


def test_every_plugin_duty_is_offered_to_the_tick():
    from salescoach import plugins
    names = {d.name for d in plugins.cron_duties()}
    assert {"followups", "replies", "calendar", "autosend", "retention", "learning", "embed"} <= names
    assert names & {"sources", "recorders"}


@pytest.mark.postgres_only
def test_a_tick_while_the_scheduler_lock_is_held_runs_nothing(db):
    ran = []
    duty = Duty("probe", lambda conn: ran.append(1), lambda: 600.0)
    holder = ops.Leader()
    assert holder.try_acquire()                    # another tick, or a scheduler process
    try:
        result = cron.tick(duties=[duty])
        assert result["ran"] == [] and "holds the scheduler lock" in result["skipped"] and ran == []
    finally:
        holder.release()
    assert cron.tick(duties=[duty])["ran"] == ["probe"]


@pytest.mark.postgres_only
def test_a_drain_connection_is_its_own_and_guarded(db):
    conn = cron._dedicated()
    try:
        assert conn.execute("SHOW idle_session_timeout").fetchone()[0] == f"{cron.SESSION_IDLE_S // 60}min"
        assert conn.actor is None
    finally:
        conn.close()
    assert stores._pg_pools                        # the pool is untouched by it; the drain never holds a pooled lock
