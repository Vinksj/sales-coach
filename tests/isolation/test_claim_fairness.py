"""One owner's backlog cannot starve every other owner's events (Postgres).

bus._claim_postgres listed the top CLAIM_SCAN claimable events. With CLAIM_SCAN or more of rep A's ahead of
rep B's one (same or higher priority) and one of A's running under A's owner lock, a free worker saw only A's
locked candidates and claimed nothing: B waited for A's whole backlog. The scan is one candidate per owner now.
"""
import time

import pytest

from salescoach.orchestrator import bus
from salescoach.schemas.events import Event
from salescoach.store import stores

pytestmark = pytest.mark.postgres_only


def _publish(conn, owner, n, priority=bus.PRIORITY_NORMAL):
    ids = []
    for i in range(n):
        ev = Event(type="T", entity_id=f"user:{owner}", dedupe_key=f"T:{owner}:{i}:{time.monotonic_ns()}")
        assert bus.publish(conn, ev, priority=priority)
        ids.append(ev.event_id)
    conn.commit()
    return ids


def test_a_free_worker_claims_another_owners_event_past_a_long_backlog(db, bus_rows):
    a_events = _publish(bus_rows, "u-a", bus.CLAIM_SCAN + 10, priority=bus.PRIORITY_INTERACTIVE)
    b_event = _publish(bus_rows, "u-b", 1, priority=bus.PRIORITY_INTERACTIVE)[0]
    worker1, worker2 = stores.sales(), stores.sales()
    try:
        with worker1.as_system(), worker2.as_system():
            first = bus.claim_next(worker1)
            assert first is not None and first.event_id == a_events[0]      # priority, then age: A's oldest
            got = bus.claim_next(worker2)
            assert got is not None and got.event_id == b_event               # B's, not nothing
    finally:
        bus.release_owner_locks(worker1)
        bus.release_owner_locks(worker2)
        worker1.close()
        worker2.close()


def test_priority_order_holds_across_owners(db, bus_rows):
    low = _publish(bus_rows, "u-a", 3, priority=bus.PRIORITY_BACKFILL)
    high = _publish(bus_rows, "u-b", 1, priority=bus.PRIORITY_INTERACTIVE)[0]
    normal = _publish(bus_rows, "u-c", 1)[0]
    worker = stores.sales()
    try:
        with worker.as_system():
            order = []
            for _ in range(3):
                ev = bus.claim_next(worker)
                order.append(ev.event_id)
                bus.complete(worker, ev.event_id)
            assert order == [high, normal, low[0]]
    finally:
        bus.release_owner_locks(worker)
        worker.close()
