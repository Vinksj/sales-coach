"""Phase 6: the bus is fair across owners and ordered by priority.

One owner has one running event at a time (a per-owner advisory lock on Postgres, the write lock plus a
NOT EXISTS on SQLite), so a bulk import by A cannot starve B, and A's events are handled in id order.
Higher priority is claimed first; equal priority is oldest first.
"""
import threading
import time

import pytest

from salescoach.orchestrator import bus, worker
from salescoach.schemas.events import Event
from salescoach.store import stores


def _publish(conn, owner, n=1, priority=bus.PRIORITY_NORMAL, kind="T"):
    ids = []
    for i in range(n):
        ev = Event(type=kind, entity_id=f"user:{owner}", dedupe_key=f"{kind}:{owner}:{i}:{time.monotonic_ns()}")
        assert bus.publish(conn, ev, priority=priority)
        ids.append(ev.event_id)
    conn.commit()
    return ids


def test_publish_records_owner_and_priority(db):
    db.execute("INSERT INTO nodes(id,type,owner_id) VALUES ('call-x','call','u-a')")
    bus.publish(db, Event(type="CALL_ENDED", entity_id="call-x", dedupe_key="CE:x"))
    bus.publish(db, Event(type="X", entity_id="user:u-b", dedupe_key="X:b"), priority=bus.PRIORITY_INTERACTIVE)
    bus.publish(db, Event(type="Y", entity_id=None, payload={"owner_id": "u-c"}, dedupe_key="Y:c"),
                priority=bus.PRIORITY_BACKFILL)
    bus.publish(db, Event(type="Z", dedupe_key="Z:actor"))            # no hint: the acting user
    db.commit()
    rows = {r["dedupe_key"]: (r["owner"], r["priority"]) for r in db.execute("SELECT * FROM wf_events")}
    assert rows == {"CE:x": ("u-a", 0), "X:b": ("u-b", 10), "Y:c": ("u-c", -10), "Z:actor": ("local", 0)}


def test_priority_then_age_decides_the_claim_order(db):
    low = _publish(db, "u-a", priority=bus.PRIORITY_BACKFILL)[0]
    normal = _publish(db, "u-b")[0]
    high = _publish(db, "u-c", priority=bus.PRIORITY_INTERACTIVE)[0]
    normal2 = _publish(db, "u-d")[0]
    order = []
    while True:
        ev = bus.claim_next(db)
        if ev is None:
            break
        order.append(ev.event_id)
        bus.complete(db, ev.event_id)
    assert order == [high, normal, normal2, low]


def test_one_running_event_per_owner_and_the_second_claimer_serves_the_other_owner(db):
    a1, a2, a3 = _publish(db, "u-a", 3)
    (b1,) = _publish(db, "u-b")
    first = bus.claim_next(db)                              # A's first: running, owner A busy
    assert first.event_id == a1
    other = stores.sales()
    try:
        second = bus.claim_next(other)                      # A's second must not run: B's goes instead
        assert second is not None and second.event_id == b1
        assert bus.claim_next(other) is None                # nothing else is claimable: A is busy
        bus.complete(other, b1)
        assert bus.claim_next(other) is None                # still busy
        bus.complete(db, a1)                                # A's lock is released with the settle
        nxt = bus.claim_next(other)
        assert nxt.event_id == a2                           # in id order, never a3 first
        assert bus.claim_next(db) is None                   # A busy again (held by `other` now)
        bus.fail(other, a2, "boom")                         # a failure releases the owner too
        assert bus.claim_next(db).event_id == a3
        bus.complete(db, a3)
    finally:
        bus.release_owner_locks(other)
        other.close()


def test_two_claimers_racing_for_one_owner_never_both_win(db):
    """Two threads claim as fast as they can; at no point do two of one owner's events run at once, and
    each owner's events are handled in id order. Every event is handled exactly once."""
    for owner in ("u-a", "u-b", "u-c"):
        _publish(db, owner, 12)
    running, lock, errors, order = {}, threading.Lock(), [], []
    start = threading.Barrier(2)

    def claimer():
        conn = stores.sales()
        try:
            start.wait()
            idle = 0
            while idle < 5:
                ev = bus.claim_next(conn)
                if ev is None:
                    idle += 1
                    time.sleep(0.02)
                    continue
                idle = 0
                owner = ev.entity_id[len("user:"):]
                with lock:
                    assert owner not in running, f"{owner} had two running events"
                    running[owner] = ev.event_id
                    order.append((owner, ev.event_id))
                time.sleep(0.003)
                with lock:
                    del running[owner]
                bus.complete(conn, ev.event_id)
        except BaseException as exc:                        # pragma: no cover - reported below
            errors.append(exc)
        finally:
            bus.release_owner_locks(conn)
            conn.close()

    threads = [threading.Thread(target=claimer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors
    assert len(order) == 36 and len({e for _, e in order}) == 36
    ids = {r["event_id"]: r["id"] for r in db.execute("SELECT event_id, id FROM wf_events")}
    for owner in ("u-a", "u-b", "u-c"):
        mine = [ids[e] for o, e in order if o == owner]
        assert mine == sorted(mine), owner                  # in id order per owner
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE status='done'").fetchone()[0] == 36


@pytest.mark.postgres_only
def test_recover_running_leaves_another_workers_live_event_alone(db):
    """A second worker process starting up must not reset an event the first is handling right now."""
    (a1,) = _publish(db, "u-a")
    (b1,) = _publish(db, "u-b")
    live = bus.claim_next(db)                               # this session holds A's lock
    assert live.event_id == a1
    db.execute("UPDATE wf_events SET status='running' WHERE event_id=?", (b1,))     # died elsewhere
    db.commit()
    other = stores.sales()
    try:
        bus.recover_running(other)
        rows = {r["event_id"]: r["status"] for r in other.execute("SELECT event_id, status FROM wf_events")}
        assert rows[a1] == "running" and rows[b1] == "pending"
    finally:
        other.close()
    bus.complete(db, a1)


def test_worker_drain_handles_events_of_two_owners(db, monkeypatch):
    seen = []
    from salescoach.orchestrator import workflow
    monkeypatch.setitem(workflow.HANDLERS, "T", [lambda conn, ev: seen.append(ev.entity_id)])
    _publish(db, "local", 2)
    assert worker.drain(db) == 2 and seen == ["user:local", "user:local"]


def test_queue_depth_per_owner(db):
    _publish(db, "u-a", 3)
    _publish(db, "u-b")
    ev = bus.claim_next(db)
    depth = {d["owner"]: d for d in bus.queue_depth(db)}
    assert depth["u-a"]["pending"] == 2 and depth["u-a"]["running"] == 1 and depth["u-b"]["pending"] == 1
    bus.complete(db, ev.event_id)
