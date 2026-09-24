"""stores.py: sales.db, the seller's store.

Built on the small graph/event engine in store/engine.py: every node mutation emits an event,
so the audit log cannot drift from the tables. The optional Jarvis integration reads a second
store (world.db) with the same engine; nothing here requires it.

Two backends (docs/architecture.md, "Two backends"):
  * SQLite, the local install: sales() opens the file, creates or migrates the schema
    (PRAGMA user_version), re-applies the plugin DDL and reconciles plugin columns on every
    connect. engine.connect() sets no busy_timeout; the web server, the workflow worker and the
    live pipeline all write this file, so every handle here sets one.
  * Postgres, when DATABASE_URL (or the path handed in) is a postgresql:// URL: sales() hands out a
    pooled connection (psycopg_pool) and, once per process, checks that the schema is at the
    version this build expects (store/pgmigrate.py, `salescoach migrate`). No DDL ever runs from
    here on Postgres.
Both return a store/db.py Connection with the sqlite3-shaped surface the code base uses.
"""
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path

from .. import config
from . import db, engine  # noqa: F401  (engine: the graph/event engine, vendored in this package)

WORLD_DB = Path(os.environ.get("WORLD_DB", os.path.expanduser("~/.claude/jarvis/world.db")))
SCHEMA = Path(__file__).with_name("schema-sales.sql")
PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
SCHEMA_VERSION = 6

log = logging.getLogger("salescoach.store")


def db_path():
    """Where the store is: DATABASE_URL (a postgresql:// URL) wins, then SALES_DB, then the data folder."""
    url = os.environ.get("DATABASE_URL")
    if url and db.is_postgres_url(url):
        return url
    return Path(os.environ.get("SALES_DB", config.DATA_DIR / "sales.db"))


def sales(path=None) -> db.Connection:
    """Writable handle on the store; on SQLite initialises the schema on first use."""
    target = path if path is not None else db_path()
    if isinstance(target, str) and db.is_postgres_url(target):
        return _postgres(target)
    return _sqlite(Path(target))


# ---- SQLite ------------------------------------------------------------------------------------

def _sqlite(path: Path) -> db.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = engine.connect(str(path))
    conn.execute("PRAGMA busy_timeout = 30000")
    if not conn.table_exists("calls"):
        engine.init(conn, schema=str(SCHEMA))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    from . import migrate
    migrate.run(conn)
    if os.environ.get("SALESCOACH_NO_PLUGINS") != "1":
        for sql_file in sorted(PLUGINS_DIR.glob("*.sql")):
            try:
                text = sql_file.read_text()
                conn.executescript(text)                   # CREATE ... IF NOT EXISTS only, so re-running is free
                reconcile_columns(conn, text)
            except sqlite3.Error as exc:
                # A broken plugin schema must never take the core store down with it.
                log.error("plugin schema %s failed: %s", sql_file.name, exc)
    return conn


def reconcile_columns(conn, schema_sql: str) -> list:
    """Add columns a plugin table gained after an install first created it. SQLite only: the
    Postgres baseline is generated from the same files, so a Postgres table already has them.

    Plugin tables are CREATE IF NOT EXISTS, which never alters an existing table, so an older
    install would keep opening a table the code no longer matches. The tracked SQL is applied to
    a scratch in-memory database and each table's columns compared with the real one; a missing
    column is added with its declared type and default. A NOT NULL column without a default
    cannot be added in place and is reported instead. Returns the "table.column" names added.
    """
    if conn.dialect != db.SQLITE:
        return []
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(schema_sql)
        added = []
        for (table,) in scratch.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue                                    # not in this database: nothing to reconcile
            for _, name, ctype, notnull, default, _pk in scratch.execute(f"PRAGMA table_info({table})"):
                if name in have:
                    continue
                if notnull and default is None:
                    log.error("plugin table %s gained NOT NULL column %s without a default; add a migration",
                              table, name)
                    continue
                decl = f"{name} {ctype or ''}".strip()
                if notnull:
                    decl += " NOT NULL"
                if default is not None:
                    decl += f" DEFAULT {default}"
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")
                added.append(f"{table}.{name}")
        if added:
            conn.commit()
        return added
    finally:
        scratch.close()


# ---- Postgres ----------------------------------------------------------------------------------

_pg_lock = threading.Lock()
_pg_pools: dict = {}
_pg_verified: set = set()
# Tests point every connection at a schema of their own (tests/conftest.py): the name is set on the
# search_path of each pooled connection as it is handed out. `_pg_schema_hook`, when set, is called
# with the URL the first time a test opens the store, so a schema is only created for tests that use one.
_pg_schema: str | None = None
_pg_schema_hook = None
_pg_prepare = True                         # False disables server-side prepared statements (tests)
_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def pool_size() -> int:
    return int(os.environ.get("SALESCOACH_PG_POOL_SIZE", "16"))


def _pool(url: str):
    with _pg_lock:
        pool = _pg_pools.get(url)
        if pool is None:
            from psycopg_pool import ConnectionPool
            kwargs = {"autocommit": True}
            if not _pg_prepare:
                kwargs["prepare_threshold"] = None
            pool = ConnectionPool(url, min_size=1, max_size=pool_size(), kwargs=kwargs, open=True,
                                  name="salescoach", timeout=30)
            _pg_pools[url] = pool
        return pool


def _postgres(url: str) -> db.Connection:
    schema = _pg_schema
    if schema is None and _pg_schema_hook is not None:
        schema = _pg_schema_hook(url)
    pool = _pool(url)
    raw = pool.getconn(timeout=30)
    conn = db.PostgresConnection(raw, release=pool.putconn)
    try:
        if schema:
            if not _SCHEMA_NAME.match(schema):
                raise ValueError(f"bad schema name {schema!r}")
            conn.execute(f'SET search_path TO "{schema}"')
        key = (url, schema)
        if key not in _pg_verified:
            from . import pgmigrate
            pgmigrate.assert_current(conn)
            _pg_verified.add(key)
    except Exception:
        conn.close()
        raise
    return conn


def forget_verified(url=None, schema=None) -> None:
    """Drop the "schema checked" memo (after migrating, or when a test schema goes away)."""
    if url is None and schema is None:
        _pg_verified.clear()
    else:
        _pg_verified.discard((url, schema))


def close_pools() -> None:
    with _pg_lock:
        for pool in _pg_pools.values():
            try:
                pool.close()
            except Exception:
                pass
        _pg_pools.clear()
        _pg_verified.clear()


# ---- world.db (Jarvis, SQLite only) -------------------------------------------------------------

def _read_only(path: Path) -> sqlite3.Connection:
    """Read-only handle on a WAL database that may have been checkpointed.

    mode=ro fails when the -shm/-wal sidecars are absent after a clean close,
    so fall back to a normal open with query_only (same fix as
    knowledge/stores.py).
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def world_ro() -> sqlite3.Connection:
    """Read-only handle on Jarvis's operator store. Any write raises."""
    return _read_only(WORLD_DB)


def world_rw() -> db.Connection:
    """Writable world.db handle for the jarvis bridge ONLY.

    Used for exactly two writes: the keep-alive event that stops the 14-day
    sweep from dropping mirrored sales loops, and the state keys that tell
    jarvis-evening which meetings belong to sales. Commitments themselves are
    landed and closed through jarvis's own commitments.py.
    """
    conn = engine.connect(str(WORLD_DB))
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def world_available() -> bool:
    return WORLD_DB.exists()


# ---- small helpers used across the package ---------------------------------

def now() -> str:
    return engine.now()


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key, value):
    conn.execute(
        "INSERT INTO state(key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now()))
