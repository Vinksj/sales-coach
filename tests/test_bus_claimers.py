"""The bus under two workers, and the not_before clock that replaced julianday().

Two claimers on their own connections (the worker and a second `salescoach work`, or two worker
processes on Postgres) must hand out every event exactly once: FOR UPDATE SKIP LOCKED on Postgres,
the write lock (BEGIN IMMEDIATE) on SQLite.
"""
import threading

from salescoach.orchestrator import bus
from salescoach.schemas.events import Event
from salescoach.store import stores
from salescoach.store.stores import now


def _publish(conn, n):
    for i in range(n):
        assert bus.publish(conn, Event(type="T", entity_id=f"e{i}", dedupe_key=f"T:{i}"))
    conn.commit()


def test_two_concurrent_claimers_claim_each_event_once(db):
    _publish(db, 40)
    claimed, lock, errors = [], threading.Lock(), []
    start = threading.Barrier(2)

    def claimer():
        conn = stores.sales()
        try:
            start.wait()
            while True:
                ev = bus.claim_next(conn)
                if ev is None:
                    break
                with lock:
                    claimed.append(ev.event_id)
                bus.complete(conn, ev.event_id)
        except Exception as exc:                       # pragma: no cover - reported below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=claimer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert len(claimed) == 40 and len(set(claimed)) == 40
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE status='done'").fetchone()[0] == 40


def test_a_claimed_event_is_invisible_to_a_second_connection_until_settled(db):
    _publish(db, 1)
    ev = bus.claim_next(db)                            # running now
    other = stores.sales()
    try:
        assert bus.claim_next(other) is None
        bus.fail(db, ev.event_id, "boom")              # back to pending, waits attempts x backoff
        assert bus.claim_next(other) is None
        db.execute("UPDATE wf_events SET not_before=NULL WHERE event_id=?", (ev.event_id,))
        db.commit()
        assert bus.claim_next(other).event_id == ev.event_id
    finally:
        other.close()


def test_not_before_backoff(db, monkeypatch):
    monkeypatch.setattr(bus, "RETRY_BACKOFF_S", 600)
    _publish(db, 1)
    ev = bus.claim_next(db)
    assert bus.fail(db, ev.event_id, "boom") == "pending"
    row = db.execute("SELECT attempts, not_before, status FROM wf_events").fetchone()
    assert row["attempts"] == 1 and row["not_before"] > now() and row["status"] == "pending"
    assert bus.claim_next(db) is None                  # 600 s away
    db.execute("UPDATE wf_events SET not_before='2000-01-01T00:00:00+00:00'")
    db.commit()
    assert bus.claim_next(db).event_id == ev.event_id  # the clock, not updated_at, decides
    bus.defer(db, ev.event_id, 3600, "quota")
    row = db.execute("SELECT attempts, not_before FROM wf_events").fetchone()
    assert row["attempts"] == 1 and row["not_before"] > now()      # defer gave the attempt back
    assert bus.claim_next(db) is None
    bus.defer(db, ev.event_id, 0, "now")
    row = db.execute("SELECT attempts FROM wf_events").fetchone()
    assert row["attempts"] == 0                        # never below zero
    assert bus.claim_next(db).event_id == ev.event_id


def test_recover_running_waits_its_backoff(db, monkeypatch):
    monkeypatch.setattr(bus, "RETRY_BACKOFF_S", 600)
    _publish(db, 2)
    first = bus.claim_next(db)
    bus.recover_running(db)                            # the worker died with `first` running
    rows = {r["event_id"]: r for r in db.execute("SELECT * FROM wf_events")}
    assert rows[first.event_id]["status"] == "pending" and rows[first.event_id]["not_before"] > now()
    assert rows[first.event_id]["error"].startswith("the worker died")
    nxt = bus.claim_next(db)
    assert nxt is not None and nxt.event_id != first.event_id      # the untouched one goes first
    db.execute("UPDATE wf_events SET attempts=3, status='running' WHERE event_id=?", (first.event_id,))
    db.commit()
    bus.recover_running(db)
    assert db.execute("SELECT status FROM wf_events WHERE event_id=?", (first.event_id,)).fetchone()[0] == "failed"


def test_publish_duplicate_does_not_poison_the_callers_transaction(db):
    db.execute("INSERT INTO nodes(id,type) VALUES ('n1','call')")
    assert bus.publish(db, Event(type="CALL_ENDED", entity_id="n1", dedupe_key="CALL_ENDED:n1"))
    assert not bus.publish(db, Event(type="CALL_ENDED", entity_id="n1", dedupe_key="CALL_ENDED:n1"))
    db.execute("INSERT INTO nodes(id,type) VALUES ('n2','call')")   # still inside the same transaction
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM wf_events").fetchone()[0] == 1
    assert bus.publish(db, Event(type="X", entity_id="n1"))          # no dedupe key: always stored
    assert bus.publish(db, Event(type="X", entity_id="n1"))
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM wf_events").fetchone()[0] == 3
