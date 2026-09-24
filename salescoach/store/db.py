"""One connection surface over SQLite and Postgres.

The code base was written against sqlite3: `conn.execute(sql, params)` with `?` placeholders,
rows addressable by name and by index, `conn.commit()` / `conn.rollback()`, `conn.in_transaction`,
and SQL strings executed for BEGIN / COMMIT / SAVEPOINT. This module keeps that surface and adds
what the two backends need to agree on:

  connect(url_or_path)   SQLiteConnection for a file path, PostgresConnection for a postgresql:// URL
  conn.dialect           "sqlite" | "postgres"
  conn.serialize(key)    take the write lock (BEGIN IMMEDIATE) / an advisory transaction lock
  conn.lock_rows(sql, params, skip_locked=False)   the same SELECT under a row lock (FOR UPDATE)
  insert_id(cursor)      the new row's id after an INSERT (lastrowid / RETURNING id)
  conn.table_exists(t), conn.columns(t)            instead of sqlite_master and PRAGMA table_info
  like(column, ci=)      a LIKE fragment with the same case rule on both backends
  Error, IntegrityError, OperationalError          tuples that catch either driver's exception

The Postgres side translates each statement once (cached by the SQL text) with a small tokenizer,
not regexes over the whole string: `?` -> `%s`, `:name` -> `%(name)s`, `%` -> `%%`,
`INSERT OR IGNORE` -> `ON CONFLICT DO NOTHING`, `IS ?` -> `IS NOT DISTINCT FROM %s`,
`BEGIN IMMEDIATE` -> `BEGIN`, SQLite's NULL ordering made explicit (NULLS FIRST on ASC, NULLS LAST
on DESC), and `RETURNING id` appended to an INSERT into a table with an identity column so
insert_id() has something to read. Everything else must already be in the dialect subset
described in docs/architecture.md ("Two backends"); the translator refuses what it cannot make
portable (INSERT OR REPLACE, PRAGMAs it does not know) rather than guess.

Transaction semantics follow sqlite3's legacy mode on both backends: a DML statement opens a
transaction implicitly, reads outside a transaction run in autocommit, commit()/rollback() end it,
and a SAVEPOINT outside a transaction opens one (its RELEASE commits). On Postgres a failed
statement aborts the transaction: code that catches a database error must roll back (or use
ON CONFLICT) instead of carrying on, which is the one rule the subset adds.
"""
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

try:
    import psycopg
    from psycopg import errors as pg_errors
    from psycopg.pq import TransactionStatus as _TS
except ImportError:                                     # a SQLite-only install
    psycopg = None
    pg_errors = None
    _TS = None

SQLITE = "sqlite"
POSTGRES = "postgres"

# `except db.IntegrityError:` catches either driver's. Tuples, not a new class: the SQLite driver's own
# exceptions keep flowing unchanged (nothing is caught and re-raised), so SQLite behaviour is untouched.
if psycopg is not None:
    Error = (sqlite3.Error, psycopg.Error)
    IntegrityError = (sqlite3.IntegrityError, pg_errors.IntegrityError)
    OperationalError = (sqlite3.OperationalError, psycopg.OperationalError)
    ProgrammingError = (sqlite3.ProgrammingError, psycopg.ProgrammingError)
else:
    Error = (sqlite3.Error,)
    IntegrityError = (sqlite3.IntegrityError,)
    OperationalError = (sqlite3.OperationalError,)
    ProgrammingError = (sqlite3.ProgrammingError,)


class NotSupported(sqlite3.OperationalError if psycopg is None else psycopg.OperationalError):
    """A statement outside the portable subset reached the Postgres translator."""


def is_postgres_url(value) -> bool:
    return isinstance(value, str) and value.startswith(("postgresql://", "postgres://"))


# ---- rows -----------------------------------------------------------------------------------------

class RowKeyError(IndexError, KeyError):
    """sqlite3.Row raises IndexError for an unknown column name; dict-minded callers catch KeyError."""


class Row:
    """A result row: `row["col"]`, `row[0]`, `row[1:]`, `dict(row)`, `row.keys()`, iteration over the
    values, `len(row)`, tuple unpacking. The same surface as sqlite3.Row (names are matched
    case-insensitively as a fallback, as sqlite3.Row does)."""
    __slots__ = ("_names", "_index", "_values")

    def __init__(self, names, values, index=None):
        self._names = names
        self._index = index if index is not None else {n: i for i, n in enumerate(names)}
        self._values = tuple(values)

    def keys(self):
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._index[key]]
            except KeyError:
                low = key.lower()
                for name, i in self._index.items():
                    if name.lower() == low:
                        return self._values[i]
                raise RowKeyError(f"No item with that key: {key!r}") from None
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._values == other._values and tuple(self._names) == tuple(other._names)
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented

    def __hash__(self):
        return hash(self._values)

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))!r})"


class Cursor:
    """The result of one Postgres statement, fully fetched: the same fetch surface as sqlite3.Cursor."""
    __slots__ = ("description", "rowcount", "insert_id", "_rows", "_pos")

    def __init__(self, description, rows, rowcount, insert_id=None):
        self.description = description
        self.rowcount = rowcount
        self.insert_id = insert_id
        self._rows = rows
        self._pos = 0

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size=1):
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self):
        self._rows = []

    @property
    def lastrowid(self):
        raise NotSupported("lastrowid is SQLite-only; use db.insert_id(cursor)")


def insert_id(cur):
    """The id the INSERT that produced `cur` created (None when nothing was inserted on Postgres)."""
    if isinstance(cur, Cursor):
        return cur.insert_id
    return cur.lastrowid


def like(column: str, ci: bool = False) -> str:
    """`column LIKE ?`. With ci=True both sides are lower-cased, which is what SQLite's LIKE does for
    ASCII on its own and Postgres's does not; the caller passes the pattern as the parameter."""
    return f"LOWER({column}) LIKE LOWER(?)" if ci else f"{column} LIKE ?"


# ---- the translator ------------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<ws>\s+|--[^\n]*)                       # whitespace and line comments
  | (?P<str>'(?:[^']|'')*')                    # string literal, '' escapes
  | (?P<ident>"(?:[^"]|"")*")                  # quoted identifier
  | (?P<param>\?|(?<![:\w]):[A-Za-z_]\w*)      # ? or :name (never ::cast)
  | (?P<num>\d+(?:\.\d*)?)
  | (?P<word>[A-Za-z_]\w*)
  | (?P<pct>%)
  | (?P<punct>\|\||<=|>=|<>|!=|::|[(),;.=<>+\-*/])
""", re.X)

_DML = {"INSERT", "UPDATE", "DELETE", "REPLACE"}
_TERM_END = {"LIMIT", "OFFSET", "FOR", "UNION", "EXCEPT", "INTERSECT"}


@dataclass(frozen=True)
class Translated:
    sql: str
    kind: str              # first keyword, upper-cased
    is_dml: bool
    savepoint: str | None  # the name for SAVEPOINT / RELEASE / ROLLBACK TO
    auto_returning: bool   # RETURNING id was appended for insert_id()


def _tokens(sql: str) -> list:
    out, pos = [], 0
    for m in _TOKEN.finditer(sql):
        if m.start() != pos:
            raise NotSupported(f"cannot tokenize SQL at offset {pos}: {sql[pos:pos + 20]!r}")
        out.append([m.lastgroup, m.group()])
        pos = m.end()
    if pos != len(sql):
        raise NotSupported(f"cannot tokenize SQL at offset {pos}: {sql[pos:pos + 20]!r}")
    return out


def _sig(tokens, start=0, step=1):
    """Indexes of significant (non-whitespace) tokens from `start`."""
    i = start
    while 0 <= i < len(tokens):
        if tokens[i][0] != "ws":
            yield i
        i += step


def _word(tok, *names) -> bool:
    return tok[0] == "word" and tok[1].upper() in names


def _order_by_nulls(tokens, not_null) -> None:
    """Postgres sorts NULLs last on ASC and first on DESC; SQLite the other way round. Make SQLite's
    rule explicit on every ORDER BY term that does not say, except bare columns known NOT NULL."""
    i = 0
    while i < len(tokens):
        if _word(tokens[i], "ORDER"):
            nxt = next(_sig(tokens, i + 1), None)
            if nxt is not None and _word(tokens[nxt], "BY"):
                i = _rewrite_terms(tokens, nxt + 1, not_null)
                continue
        i += 1


def _rewrite_terms(tokens, start, not_null) -> int:
    depth, term = 0, []
    i = start
    while True:
        at_end = i >= len(tokens)
        tok = tokens[i] if not at_end else None
        boundary = at_end or (depth == 0 and (tok[1] in (",", ";", ")") or _word(tok, *_TERM_END)))
        if boundary:
            _finish_term(tokens, term, not_null)
            term = []
            if at_end or tok[1] in (";", ")") or _word(tok, *_TERM_END):
                return i
            i += 1
            continue
        if tok[1] == "(":
            depth += 1
        elif tok[1] == ")":
            depth -= 1
        if tok[0] != "ws":
            term.append(i)
        i += 1


def _finish_term(tokens, term, not_null) -> None:
    if not term:
        return
    words = [tokens[j][1].upper() for j in term if tokens[j][0] == "word"]
    if "NULLS" in words:
        return
    direction = "ASC"
    last = term[-1]
    if _word(tokens[last], "ASC", "DESC"):
        direction = tokens[last][1].upper()
        body = term[:-1]
    else:
        body = term
    # a bare column (`col`, `t.col`) that is NOT NULL everywhere needs nothing: keeps index order usable
    if body and tokens[body[-1]][0] == "word" and tokens[body[-1]][1] in not_null and (
            len(body) == 1 or (len(body) == 3 and tokens[body[1]][1] == "." and tokens[body[0]][0] == "word")):
        return
    tokens[last][1] += " NULLS FIRST" if direction == "ASC" else " NULLS LAST"


def _translate(sql: str, identity_tables, not_null) -> Translated:
    tokens = _tokens(sql)
    sig = list(_sig(tokens))
    if not sig:
        return Translated(sql, "", False, None, False)
    first = tokens[sig[0]]
    kind = first[1].upper() if first[0] == "word" else ""
    savepoint = None
    auto_returning = False

    if kind == "BEGIN":                                   # BEGIN [DEFERRED|IMMEDIATE|EXCLUSIVE]
        return Translated("BEGIN", kind, False, None, False)
    if kind in ("SAVEPOINT", "RELEASE"):
        j = sig[1] if len(sig) > 1 else None
        if j is not None and _word(tokens[j], "SAVEPOINT"):
            j = sig[2] if len(sig) > 2 else None
        savepoint = tokens[j][1] if j is not None else None
    if kind == "ROLLBACK" and len(sig) > 1 and _word(tokens[sig[1]], "TO"):
        j = sig[2] if len(sig) > 2 else None
        if j is not None and _word(tokens[j], "SAVEPOINT"):
            j = sig[3] if len(sig) > 3 else None
        savepoint = tokens[j][1] if j is not None else None
    if kind == "INSERT" and len(sig) > 2 and _word(tokens[sig[1]], "OR"):
        conflict = tokens[sig[2]][1].upper()
        if conflict != "IGNORE":
            raise NotSupported(f"INSERT OR {conflict} is SQLite-only; write ON CONFLICT ... DO UPDATE")
        tokens[sig[1]][1] = tokens[sig[2]][1] = ""
        kind_extra = " ON CONFLICT DO NOTHING"
    else:
        kind_extra = ""
    if kind == "REPLACE":
        raise NotSupported("REPLACE INTO is SQLite-only; write ON CONFLICT ... DO UPDATE")

    is_dml = kind in _DML or (kind == "WITH" and any(_word(t, *_DML) for t in tokens))

    # `x IS y` / `x IS NOT y` with y a parameter or a column: SQLite's null-safe (in)equality; Postgres
    # spells it IS [NOT] DISTINCT FROM. `IS NULL`, `IS NOT NULL`, `IS TRUE` ... stay what they are.
    for n, i in enumerate(sig):
        if _word(tokens[i], "IS") and n + 1 < len(sig):
            nxt = tokens[sig[n + 1]]
            if _word(nxt, "NOT"):
                after = tokens[sig[n + 2]] if n + 2 < len(sig) else None
                if after is not None and not _word(after, "NULL", "TRUE", "FALSE", "UNKNOWN", "DISTINCT"):
                    tokens[i][1] = "IS DISTINCT FROM"
                    nxt[1] = ""
            elif not _word(nxt, "NULL", "TRUE", "FALSE", "UNKNOWN", "DISTINCT"):
                tokens[i][1] = "IS NOT DISTINCT FROM"

    _order_by_nulls(tokens, not_null)

    for tok in tokens:
        if tok[0] == "param":
            tok[1] = "%s" if tok[1] == "?" else f"%({tok[1][1:]})s"
        elif tok[0] == "pct":
            tok[1] = "%%"
        elif tok[0] == "str":
            tok[1] = tok[1].replace("%", "%%")

    out = "".join(t[1] for t in tokens)
    if kind == "INSERT":
        m = re.match(r"\s*INSERT\s+(?:OR\s+IGNORE\s+)?\s*INTO\s+\"?(\w+)\"?", sql, re.I | re.S)
        table = m.group(1) if m else None
        out = out.rstrip().rstrip(";")
        if kind_extra:
            out += kind_extra
        if table in identity_tables and not any(_word(t, "RETURNING") for t in tokens):
            out += " RETURNING id"
            auto_returning = True
    return Translated(out, kind, is_dml, savepoint, auto_returning)


@lru_cache(maxsize=4096)
def translate(sql: str) -> Translated:
    """SQLite-flavoured SQL from the code base -> the Postgres statement, cached per statement text."""
    from . import catalog
    return _translate(sql, catalog.identity_tables(), catalog.always_not_null_columns())


# ---- connections ---------------------------------------------------------------------------------

class Connection:
    dialect = ""

    def execute(self, sql, params=()):
        raise NotImplementedError

    def executemany(self, sql, seq):
        raise NotImplementedError

    def executescript(self, sql):
        raise NotImplementedError

    def commit(self):
        raise NotImplementedError

    def rollback(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    @property
    def in_transaction(self) -> bool:
        raise NotImplementedError

    def serialize(self, key: str) -> None:
        """Serialise writers for `key` until the transaction ends. SQLite has one write lock, so this is
        BEGIN IMMEDIATE whatever the key; Postgres takes pg_advisory_xact_lock(hashtext(key))."""
        raise NotImplementedError

    def lock_rows(self, sql, params=(), skip_locked=False):
        """Run a SELECT so that the rows it returns stay ours until commit: FOR UPDATE [SKIP LOCKED] on
        Postgres, the write lock on SQLite (where a second claimer waits on busy_timeout instead)."""
        raise NotImplementedError

    def table_exists(self, name: str) -> bool:
        raise NotImplementedError

    def columns(self, table: str) -> list:
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class SQLiteConnection(Connection):
    """sqlite3.Connection, unchanged, plus the shared helpers. Unknown attributes go to the driver."""
    dialect = SQLITE

    def __init__(self, raw: sqlite3.Connection):
        self._raw = raw

    def execute(self, sql, params=()):
        return self._raw.execute(sql, params)

    def executemany(self, sql, seq):
        return self._raw.executemany(sql, seq)

    def executescript(self, sql):
        return self._raw.executescript(sql)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def cursor(self):
        return self._raw.cursor()

    @property
    def in_transaction(self) -> bool:
        return self._raw.in_transaction

    @property
    def raw(self) -> sqlite3.Connection:
        return self._raw

    def serialize(self, key: str) -> None:
        if not self._raw.in_transaction:
            self._raw.execute("BEGIN IMMEDIATE")

    def lock_rows(self, sql, params=(), skip_locked=False):
        self.serialize("")
        return self._raw.execute(sql, params)

    def table_exists(self, name: str) -> bool:
        return self._raw.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (name,)).fetchone() is not None

    def columns(self, table: str) -> list:
        return [r[1] for r in self._raw.execute(f"PRAGMA table_info({table})")]

    def __getattr__(self, name):
        return getattr(self._raw, name)


_PRAGMA_NOOP = {"busy_timeout", "foreign_keys", "journal_mode", "query_only", "synchronous"}


class PostgresConnection(Connection):
    dialect = POSTGRES

    def __init__(self, raw, release=None):
        """`raw` is a psycopg connection in autocommit mode (transactions are explicit here, as in
        sqlite3). `release`, when given, is called with the raw connection by close() instead of
        closing it: a pool's putconn."""
        if not raw.autocommit:
            raw.autocommit = True
        self._raw = raw
        self._release = release
        self._closed = False
        self._sp_opener = None         # the SAVEPOINT that opened the current transaction, if one did

    # -- transactions --

    @property
    def in_transaction(self) -> bool:
        return self._raw.info.transaction_status in (_TS.INTRANS, _TS.INERROR)

    def _begin(self):
        self._raw.execute("BEGIN")
        self._sp_opener = None

    def commit(self):
        if self.in_transaction:
            self._raw.execute("COMMIT")
        self._sp_opener = None

    def rollback(self):
        if self.in_transaction:
            self._raw.execute("ROLLBACK")
        self._sp_opener = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        raw, self._raw = self._raw, None
        if self._release is not None:
            try:
                if raw.info.transaction_status in (_TS.INTRANS, _TS.INERROR):
                    raw.execute("ROLLBACK")
            except Exception:
                pass
            self._release(raw)
        else:
            raw.close()

    def __del__(self):
        try:
            if not self._closed and self._raw is not None:
                self.close()
        except Exception:
            pass

    # -- statements --

    def execute(self, sql, params=()):
        if self._closed:
            raise psycopg.ProgrammingError("Cannot operate on a closed database.")
        t = translate(sql)
        if t.kind == "PRAGMA":
            return self._pragma(sql)
        if t.kind == "BEGIN":
            if self.in_transaction:
                raise psycopg.OperationalError("cannot start a transaction within a transaction")
            self._begin()
            return Cursor(None, [], -1)
        if t.kind == "COMMIT":
            self.commit()
            return Cursor(None, [], -1)
        if t.kind == "ROLLBACK" and t.savepoint is None:
            self.rollback()
            return Cursor(None, [], -1)
        if t.kind == "SAVEPOINT" and not self.in_transaction:
            self._begin()
            self._sp_opener = t.savepoint
        if t.is_dml and not self.in_transaction:
            self._begin()
        cur = self._raw.execute(t.sql, _adapt(params))
        if t.kind == "RELEASE" and t.savepoint and t.savepoint == self._sp_opener:
            self.commit()                                   # SQLite: releasing the outermost savepoint commits
        return self._wrap(cur, t)

    def executemany(self, sql, seq):
        t = translate(sql)
        if t.is_dml and not self.in_transaction:
            self._begin()
        with self._raw.cursor() as cur:
            cur.executemany(t.sql, [_adapt(p) for p in seq])
            return Cursor(None, [], cur.rowcount)

    def executescript(self, sql):
        """Several statements, no parameters (sqlite3 commits first; so do we)."""
        self.commit()
        self._raw.execute(sql)
        return Cursor(None, [], -1)

    def _wrap(self, cur, t: Translated) -> Cursor:
        rows, description, new_id = [], cur.description, None
        if description is not None:
            names = tuple(d.name for d in description)
            index = {n: i for i, n in enumerate(names)}
            rows = [Row(names, r, index) for r in cur.fetchall()]
        if t.auto_returning:
            new_id = rows[0][0] if rows else None
            rows = []
        return Cursor(description, rows, cur.rowcount, new_id)

    def _pragma(self, sql: str) -> Cursor:
        m = re.match(r"\s*PRAGMA\s+(\w+)(?:\s*\(\s*\"?(\w+)\"?\s*\)|\s*=\s*\S+)?\s*;?\s*$", sql, re.I)
        name = m.group(1).lower() if m else ""
        if name == "table_info" and m.group(2):
            return self._table_info(m.group(2))
        if name in _PRAGMA_NOOP:
            return Cursor(None, [], -1)
        raise NotSupported(f"PRAGMA {name or sql.strip()} is SQLite-only")

    def _table_info(self, table: str) -> Cursor:
        cur = self._raw.execute(
            "SELECT c.ordinal_position - 1, c.column_name, c.data_type, c.is_nullable = 'NO', c.column_default, "
            "COALESCE(k.ordinal_position, 0) FROM information_schema.columns c "
            "LEFT JOIN (SELECT kcu.column_name, kcu.ordinal_position FROM information_schema.table_constraints tc "
            "  JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name "
            "  AND kcu.table_schema = tc.table_schema AND kcu.table_name = tc.table_name "
            "  WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = current_schema() "
            "  AND tc.table_name = %s) k ON k.column_name = c.column_name "
            "WHERE c.table_schema = current_schema() AND c.table_name = %s ORDER BY c.ordinal_position",
            (table, table))
        names = ("cid", "name", "type", "notnull", "dflt_value", "pk")
        index = {n: i for i, n in enumerate(names)}
        rows = [Row(names, (r[0], r[1], r[2], int(r[3]), r[4], r[5]), index) for r in cur.fetchall()]
        return Cursor(cur.description, rows, len(rows))

    # -- helpers --

    def serialize(self, key: str) -> None:
        if not self.in_transaction:
            self._begin()
        self._raw.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))

    def lock_rows(self, sql, params=(), skip_locked=False):
        if not self.in_transaction:
            self._begin()
        suffix = " FOR UPDATE SKIP LOCKED" if skip_locked else " FOR UPDATE"
        return self.execute(sql.rstrip().rstrip(";") + suffix, params)

    def table_exists(self, name: str) -> bool:
        return self._raw.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = %s",
            (name,)).fetchone() is not None

    def columns(self, table: str) -> list:
        return [r[0] for r in self._raw.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = %s ORDER BY ordinal_position", (table,)).fetchall()]

    @property
    def raw(self):
        return self._raw


def _adapt_value(v):
    if isinstance(v, bool):                 # INTEGER columns hold 0/1; psycopg would send a boolean
        return int(v)
    return v


def _adapt(params):
    if params is None:
        return ()
    if isinstance(params, Mapping):
        return {k: _adapt_value(v) for k, v in params.items()}
    return [_adapt_value(v) for v in params]


def connect(url_or_path, **kwargs) -> Connection:
    """A connection to a SQLite file (row_factory=sqlite3.Row, foreign keys on) or a Postgres URL."""
    if is_postgres_url(url_or_path):
        if psycopg is None:
            raise RuntimeError("Postgres needs the psycopg package: pip install 'psycopg[binary,pool]'")
        return PostgresConnection(psycopg.connect(url_or_path, autocommit=True, **kwargs))
    raw = sqlite3.connect(str(url_or_path), **kwargs)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return SQLiteConnection(raw)
