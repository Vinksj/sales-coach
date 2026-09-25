"""Postgres schema migrations: numbered files under store/pg/, applied once, in order, then the
repeatable steps (store/pg/rls.sql), re-applied whenever their content changes.

    salescoach migrate            apply what is missing (DATABASE_MIGRATE_URL, else DATABASE_URL)
    salescoach migrate --check    exit 1 when the database is behind the code

Unlike SQLite (store/migrate.py, keyed on PRAGMA user_version and run by every connect), nothing
here runs when the app connects: stores.sales() only asserts that the database is at the version
this build expects and refuses to start otherwise. Two processes deploying at once serialise on
pg_advisory_lock(MIGRATE_LOCK); each step lands in its own transaction together with its
schema_migrations row, so a crash mid-way leaves either both or neither.

A step is `NNNN_<name>.sql` (statements, no parameters) or a Python callable registered in
PY_STEPS under the same number, which takes the connection and owns nothing transactional: the
runner wraps it. 0001 is generated from the SQLite schema by scripts/gen_pg_baseline.py. Numbers
need not be contiguous (0003 was the one-shot row-level-security file, replaced by rls.sql before
anything was deployed; a database that did apply it simply goes on to 0004): the steps are sorted
and the highest is the version this build expects.

A REPEATABLE step (REPEATABLES: name -> file) describes a state rather than a change: after the
numbered steps, in the same advisory lock, each one whose sha256 differs from the checksum recorded
in `schema_repeatables` is applied in one transaction together with its new checksum. rls.sql is the
only one: generated from tenancy.py (store/rls.py), it drops every policy of the schema and creates
the whole set again, so tables added by later numbered steps are policed too. assert_current()
refuses a database whose recorded checksum is not this build's, exactly as it refuses a version.
"""
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

PG_DIR = Path(__file__).with_name("pg")
MIGRATE_LOCK = 0x53414C45                  # 'SALE': the advisory-lock key every migrator takes
PY_STEPS: dict = {}                        # version -> callable(conn)
REPEATABLES = {"rls": PG_DIR / "rls.sql"}  # name -> file; applied in this order after the numbered steps
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


def repeatables() -> list:
    """[(name, sql, sha256 hex)] of every repeatable step, in application order."""
    out = []
    for name, path in REPEATABLES.items():
        text = path.read_text()
        out.append((name, text, hashlib.sha256(text.encode()).hexdigest()))
    return out


def _ensure_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_repeatables ("
                 "name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")


def current_version(conn) -> int:
    with conn.as_system():
        if not conn.table_exists("schema_migrations"):
            return 0
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def recorded_checksums(conn) -> dict:
    """{name: checksum} of the repeatable steps as last applied to this database ({} before the first)."""
    with conn.as_system():
        if not conn.table_exists("schema_repeatables"):
            return {}
        return {r[0]: r[1] for r in conn.execute("SELECT name, checksum FROM schema_repeatables").fetchall()}


def stale_repeatables(conn) -> list:
    """The repeatable steps whose recorded checksum is missing or differs from this build's file."""
    recorded = recorded_checksums(conn)
    return [name for name, _, digest in repeatables() if recorded.get(name) != digest]


def apply(conn, log=None) -> list:
    """Apply every step above the current version, then every repeatable step whose checksum changed.
    Returns what was applied: the version numbers, then the names of the repeatable steps."""
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
        recorded = recorded_checksums(conn)
        for name, text, digest in repeatables():
            if recorded.get(name) == digest:
                continue
            conn.execute("BEGIN")
            try:
                conn.raw.execute(text)                      # the whole file, inside this transaction
                conn.execute("INSERT INTO schema_repeatables(name,checksum,applied_at) VALUES (?,?,?) "
                             "ON CONFLICT(name) DO UPDATE SET checksum=excluded.checksum, applied_at=excluded.applied_at",
                             (name, digest, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                conn.execute("COMMIT")
            except Exception:
                conn.rollback()
                raise
            applied.append(name)
            if log:
                log(f"applied {name}.sql ({digest[:12]})")
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)" % MIGRATE_LOCK)
        conn.execute("RESET row_security")
    return applied


def check(conn) -> tuple:
    """(current, expected)."""
    return current_version(conn), expected_version()


def assert_current(conn) -> None:
    """Refuse (SchemaOutOfDate) a database at another version than this build's, or whose repeatable
    steps (the row-level policies) are not the ones this build ships."""
    current, expected = check(conn)
    if current != expected:
        raise SchemaOutOfDate(
            f"the Postgres schema is at version {current} and this build expects {expected}: "
            f"run `salescoach migrate`" + (" (the database is AHEAD of the code: deploy a newer build)"
                                          if current > expected else ""))
    stale = stale_repeatables(conn)
    if stale:
        raise SchemaOutOfDate(
            f"the Postgres schema's {', '.join(n + '.sql' for n in stale)} (row-level security) is not the one this "
            f"build ships: run `salescoach migrate`")
