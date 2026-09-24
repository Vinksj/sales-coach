"""Postgres schema migrations: numbered files under store/pg/, applied once, in order.

    salescoach migrate            apply what is missing (DATABASE_MIGRATE_URL, else DATABASE_URL)
    salescoach migrate --check    exit 1 when the database is behind the code

Unlike SQLite (store/migrate.py, keyed on PRAGMA user_version and run by every connect), nothing
here runs when the app connects: stores.sales() only asserts that the database is at the version
this build expects and refuses to start otherwise. Two processes deploying at once serialise on
pg_advisory_lock(MIGRATE_LOCK); each step lands in its own transaction together with its
schema_migrations row, so a crash mid-way leaves either both or neither.

A step is `NNNN_<name>.sql` (statements, no parameters) or a Python callable registered in
PY_STEPS under the same number, which takes the connection and owns nothing transactional: the
runner wraps it. 0001 is generated from the SQLite schema by scripts/gen_pg_baseline.py.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

PG_DIR = Path(__file__).with_name("pg")
MIGRATE_LOCK = 0x53414C45                  # 'SALE': the advisory-lock key every migrator takes
PY_STEPS: dict = {}                        # version -> callable(conn)
_FILE = re.compile(r"^(\d{4})_([A-Za-z0-9_-]+)\.sql$")


class SchemaOutOfDate(RuntimeError):
    pass


def steps() -> list:
    """[(version, name, script_or_callable)] in order, from the files and PY_STEPS."""
    found = {}
    for path in sorted(PG_DIR.glob("*.sql")):
        m = _FILE.match(path.name)
        if m:
            found[int(m.group(1))] = (m.group(2), path.read_text())
    for version, fn in PY_STEPS.items():
        found[version] = (getattr(fn, "__name__", f"step_{version}"), fn)
    return [(v, found[v][0], found[v][1]) for v in sorted(found)]


def expected_version() -> int:
    return max((v for v, _, _ in steps()), default=0)


def _ensure_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")


def current_version(conn) -> int:
    with conn.as_system():
        if not conn.table_exists("schema_migrations"):
            return 0
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def apply(conn, log=None) -> list:
    """Apply every step above the current version. Returns the versions applied."""
    if conn.dialect != "postgres":
        raise SchemaOutOfDate("pgmigrate runs on Postgres only; SQLite migrates in store/migrate.py")
    with conn.as_system():
        return _apply(conn, log)


def _apply(conn, log) -> list:
    conn.commit()
    # Migrations run as the owner role, which bypasses row-level security (docs/architecture.md,
    # "Isolation"). row_security = off makes a statement that a policy WOULD filter fail loudly instead
    # of silently touching no rows: an owner role without BYPASSRLS shows up here, not as a half-done backfill.
    conn.execute("SET row_security = off")
    conn.execute("SELECT pg_advisory_lock(%s)" % MIGRATE_LOCK)
    applied = []
    try:
        _ensure_table(conn)
        have = current_version(conn)
        for version, name, step in steps():
            if version <= have:
                continue
            conn.execute("BEGIN")
            try:
                if callable(step):
                    step(conn)
                else:
                    conn.raw.execute(step)                  # the whole file, inside this transaction
                conn.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)",
                             (version, name, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                conn.execute("COMMIT")
            except Exception:
                conn.rollback()
                raise
            applied.append(version)
            if log:
                log(f"applied {version:04d}_{name}")
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)" % MIGRATE_LOCK)
        conn.execute("RESET row_security")
    return applied


def check(conn) -> tuple:
    """(current, expected)."""
    return current_version(conn), expected_version()


def assert_current(conn) -> None:
    current, expected = check(conn)
    if current != expected:
        raise SchemaOutOfDate(
            f"the Postgres schema is at version {current} and this build expects {expected}: "
            f"run `salescoach migrate`" + (" (the database is AHEAD of the code: deploy a newer build)"
                                          if current > expected else ""))
