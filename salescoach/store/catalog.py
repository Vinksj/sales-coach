"""What the tracked SQLite schema declares, read from the SQL files, for code that must know it
without a database: the translator (which tables have an identity column, which columns are NOT
NULL), the Postgres baseline generator, the tenancy lint and the schema-parity test.

The SQLite schema (store/schema-sales.sql + plugins/*.sql) is the source of truth; the Postgres
baseline is generated from it (scripts/gen_pg_baseline.py) and checked against it in CI.
"""
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

STORE_DIR = Path(__file__).resolve().parent
CORE_SCHEMA = STORE_DIR / "schema-sales.sql"
PLUGINS_DIR = STORE_DIR.parent / "plugins"


@dataclass(frozen=True)
class Column:
    name: str
    type: str          # declared type, upper-cased: INTEGER | TEXT | REAL | BLOB
    notnull: bool
    default: str | None
    pk: int            # 0, or the 1-based position in the primary key


@dataclass(frozen=True)
class Index:
    name: str
    table: str
    unique: bool
    columns: tuple      # column names, in index order
    where: str | None   # partial-index predicate, if any
    origin: str         # c = CREATE INDEX, u = UNIQUE constraint, pk = PRIMARY KEY


def schema_files() -> list[Path]:
    return [CORE_SCHEMA] + sorted(PLUGINS_DIR.glob("*.sql"))


def schema_sql() -> str:
    return "\n".join(p.read_text() for p in schema_files())


@lru_cache(maxsize=1)
def _scratch() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_sql())
    return conn


@lru_cache(maxsize=1)
def tables() -> dict:
    """{table: [Column, ...]} in declaration order, every table the tracked schema creates."""
    conn = _scratch()
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                "ORDER BY name"):
        out[name] = [Column(c[1], (c[2] or "").upper(), bool(c[3]), c[4], c[5])
                     for c in conn.execute(f"PRAGMA table_info({name})")]
    return out


@lru_cache(maxsize=1)
def indexes() -> list:
    """Every index, explicit or implied by UNIQUE / a non-rowid PRIMARY KEY."""
    conn = _scratch()
    out = []
    for table in tables():
        for _, name, unique, origin, _partial in conn.execute(f"PRAGMA index_list({table})"):
            cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_xinfo({name})") if r[2] is not None)
            where = None
            if origin == "c":
                sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                m = re.search(r"\bWHERE\b(.*)$", sql, re.S | re.I)
                where = " ".join(m.group(1).split()) if m else None
            out.append(Index(name, table, bool(unique), cols, where, origin))
    return out


@lru_cache(maxsize=1)
def identity_tables() -> frozenset:
    """Tables whose `id` is INTEGER PRIMARY KEY AUTOINCREMENT (BIGSERIAL-like on Postgres)."""
    found = set()
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", schema_sql(), re.S):
        if re.search(r"^\s*id\s+INTEGER PRIMARY KEY AUTOINCREMENT", m.group(2), re.M):
            found.add(m.group(1))
    return frozenset(found)


@lru_cache(maxsize=1)
def always_not_null_columns() -> frozenset:
    """Column names that are NOT NULL (or a primary key) in EVERY table that has them. Ordering by one
    of these needs no NULLS FIRST/LAST rewrite, so an index can still serve the ORDER BY."""
    seen: dict = {}
    for cols in tables().values():
        for c in cols:
            seen[c.name] = seen.get(c.name, True) and (c.notnull or bool(c.pk))
    return frozenset(name for name, ok in seen.items() if ok)


@lru_cache(maxsize=1)
def check_counts() -> dict:
    """{table: number of CHECK constraints declared}."""
    conn = _scratch()
    out = {}
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
        out[name] = len(re.findall(r"\bCHECK\s*\(", sql, re.I))
    return out


@lru_cache(maxsize=1)
def foreign_keys() -> dict:
    """{table: frozenset(parent tables its foreign keys point at)} (self references left out)."""
    conn = _scratch()
    return {t: frozenset(r[2] for r in conn.execute(f"PRAGMA foreign_key_list({t})") if r[2] != t) for t in tables()}


def children_first(names) -> list:
    """`names` ordered so that every table comes BEFORE the tables its foreign keys point at: the order to
    delete in. Ties are alphabetical, so the order (and any SQL generated from it) is stable."""
    names = set(names)
    fks = foreign_keys()
    parents = {t: (fks.get(t, frozenset()) & names) for t in names}
    order, done = [], set()
    while len(order) < len(names):
        # a table is ready when no remaining table points at it
        ready = sorted(t for t in names - done if not any(t in parents[o] for o in names - done - {t}))
        if not ready:
            raise ValueError(f"circular foreign keys among {sorted(names - done)}")
        order.extend(ready)
        done.update(ready)
    return order
