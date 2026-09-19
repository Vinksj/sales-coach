"""stores.py: sales.db, the seller's store.

Built on the small graph/event engine in store/engine.py: every node mutation emits an event,
so the audit log cannot drift from the tables. The optional Jarvis integration reads a second
store (world.db) with the same engine; nothing here requires it.

engine.connect() sets no busy_timeout. The web server, the workflow worker and the live pipeline
all write this file, so every handle here sets one.
"""
import os
import sqlite3
from pathlib import Path

from .. import config

WORLD_DB = Path(os.environ.get("WORLD_DB", os.path.expanduser("~/.claude/jarvis/world.db")))
SCHEMA = Path(__file__).with_name("schema-sales.sql")
PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
SCHEMA_VERSION = 5


from . import engine  # noqa: E402  (the graph/event engine, vendored in this package)


def db_path() -> Path:
    return Path(os.environ.get("SALES_DB", config.DATA_DIR / "sales.db"))


def sales(path=None) -> sqlite3.Connection:
    """Writable handle on sales.db; initialises the schema on first use."""
    path = Path(path) if path else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = engine.connect(str(path))
    conn.execute("PRAGMA busy_timeout = 30000")
    fresh = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone() is None
    if fresh:
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
                import logging
                logging.getLogger("salescoach.store").error("plugin schema %s failed: %s", sql_file.name, exc)
    return conn


def reconcile_columns(conn, schema_sql: str) -> list:
    """Add columns a plugin table gained after an install first created it.

    Plugin tables are CREATE IF NOT EXISTS, which never alters an existing table, so an older
    install would keep opening a table the code no longer matches. The tracked SQL is applied to
    a scratch in-memory database and each table's columns compared with the real one; a missing
    column is added with its declared type and default. A NOT NULL column without a default
    cannot be added in place and is reported instead. Returns the "table.column" names added.
    """
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
                    import logging
                    logging.getLogger("salescoach.store").error(
                        "plugin table %s gained NOT NULL column %s without a default; add a migration", table, name)
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


def world_rw() -> sqlite3.Connection:
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
