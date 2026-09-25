"""store/catalog.py is first used from whichever thread translates a statement first.

Regression (e2e run, 2026-09-25): the catalog kept ONE cached in-memory SQLite connection, which belongs to
the thread that opened it. A worker process's loops and heartbeat translate their first statements at the
same moment, and the heartbeat died with sqlite3.ProgrammingError ("SQLite objects created in a thread can
only be used in that same thread"); any later derivation from another thread (indexes, check_counts,
foreign_keys) failed the same way. Every derivation now opens its own scratch database.
"""
import threading

from salescoach.store import catalog, db

DERIVED = (catalog.tables, catalog.indexes, catalog.identity_tables, catalog.always_not_null_columns,
           catalog.check_counts, catalog.foreign_keys)


def _clear():
    for fn in DERIVED:
        fn.cache_clear()
    db.translate.cache_clear()


def _in_threads(fn, n=8):
    errors, barrier = [], threading.Barrier(n)

    def run():
        try:
            barrier.wait()
            fn()
        except Exception as exc:          # the failure under test is an exception in a thread
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_a_derivation_made_in_one_thread_and_the_next_in_another():
    _clear()
    try:
        assert catalog.tables()                                   # this thread first
        errors = _in_threads(lambda: (catalog.indexes(), catalog.check_counts(), catalog.foreign_keys()), n=1)
        assert errors == []
    finally:
        _clear()


def test_many_threads_translating_their_first_statement_at_once():
    _clear()
    try:
        errors = _in_threads(lambda: db.translate("SELECT id FROM wf_events WHERE status=? ORDER BY id"), n=8)
        assert errors == []
        assert catalog.always_not_null_columns() and "calls" in catalog.tables()
    finally:
        _clear()
