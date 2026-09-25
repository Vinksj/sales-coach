"""Build a synthetic single-user SQLite install with the test fixtures, for the import-sqlite round trip.

    PYTHONPATH=<repo>:<repo>/tests python e2e/build_sqlite.py OUT.db

Runs on the host, in local mode, exactly as a laptop would: the conftest seller profile, the pipeline test's
deal, people and two pasted calls processed by the FakeProvider (tests/test_core_pipeline.py), plus the
learned patterns. Prints {"counts": {table: rows}} for every table import-sqlite copies.
"""
import json
import os
import sys
import tempfile
from pathlib import Path


def main(out: str) -> dict:
    path = Path(out).resolve()
    if path.exists():
        path.unlink()
    work = Path(tempfile.mkdtemp(prefix="sc-e2e-sqlite-"))
    for name in ("DATABASE_URL", "DATABASE_MIGRATE_URL", "SALESCOACH_MODE"):
        os.environ.pop(name, None)
    os.environ["SALES_DB"] = str(path)
    os.environ["SALESCOACH_DATA"] = str(work / "data")
    import conftest                                     # noqa: F401  (a configured seller profile, as in the tests)
    from salescoach import config, providers
    config.DATA_DIR = work / "data"
    config.RUNTIME_DIR = work / "runtime"
    from salescoach.learning import patterns
    from salescoach.lifecycle import importer
    from salescoach.orchestrator import worker
    from salescoach.providers.fake import FakeProvider
    from salescoach.sources import paste
    from salescoach.store import stores, tenancy
    stores.WORLD_DB = work / "absent-world.db"
    from test_core_pipeline import CALL1, CALL2, _script, _setup

    fake = FakeProvider()
    providers.set_override(fake)
    conn = stores.sales(path)
    try:
        deal, people = _setup(conn)
        _script(fake)
        paste.import_text(conn, CALL1, "NWP weekly", deal_id=deal, participants=people)
        worker.drain(conn)
        paste.import_text(conn, CALL2, "NWP second", deal_id=deal, participants=people)
        worker.drain(conn)
        patterns.recompute(conn)
        conn.commit()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in (*tenancy.tables_of(tenancy.OWNED), *importer.EXTRA_TABLES) if conn.table_exists(t)}
    finally:
        conn.close()
        providers.clear_override()
    return {"path": str(path), "counts": counts}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1])))
