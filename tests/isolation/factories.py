"""A minimal valid row for every OWNED table, for a given owner, derived from the tracked schema.

tests/isolation/test_rls_matrix.py runs the same isolation checks against EVERY table in
tenancy.tables_of(OWNED); a new OWNED table therefore needs nothing here unless its columns need
something the rules below cannot derive, in which case `insert()` raises NoFactory and the matrix
fails loudly (add an entry to VALUES or a parent to PARENTS). The rules:

  * owner_id is the owner asked for (explicitly, so that "B inserting as A" is exactly that).
  * a column named in PARENTS is a row of that parent table for the same owner, made on demand
    (nodes -> calls -> emails -> email_replies -> reply_proposals, and so on); person_id /
    account_id are directory nodes (owner NULL), made once per test.
  * a NOT NULL column without a default takes: VALUES[(table, column)] if listed, else the first
    value of its CHECK(col IN (...)) constraint, else a value by type (a unique TEXT, 1, 1.0, one byte).
  * columns with a default are left to it. TEXT primary keys and UNIQUE columns get unique values.

`insert()` returns the row's locator: {pk column: value} for `where(locator)`.
"""
import itertools
import re

from salescoach import repo
from salescoach.store import catalog, db, tenancy
from salescoach.store.stores import now


class NoFactory(RuntimeError):
    pass


_seq = itertools.count(1)


def uniq(prefix: str = "x") -> str:
    return f"{prefix}-{next(_seq)}"


# The parent a foreign-key-ish column points at: column -> (parent table, parent key column).
PARENTS = {
    "call_id": ("calls", "node_id"),
    "deal_id": ("deals", "node_id"),
    "loop_id": ("loops", "node_id"),
    "email_id": ("emails", "id"),
    "reply_id": ("email_replies", "id"),
    "run_id": ("agent_runs", "id"),
    "nudge_id": ("nudges", "id"),
    "src": ("nodes", "id"),
    "dst": ("nodes", "id"),
}
NODE_TYPES = {"calls": "call", "deals": "deal", "loops": "loop", "sources": "call", "nodes": "call"}
DIRECTORY = {"person_id": "person", "account_id": "account"}

# Values the rules cannot derive, or that must be a particular thing for the row to make sense.
VALUES = {
    ("nodes", "type"): "call",                      # an OWNED node; account/person are the directory
    ("turns", "tier"): "final",
    ("turns", "channel"): "me",
    ("speakers", "channel"): "me",
    ("agent_runs", "status"): "ok",
    ("emails", "status"): "drafted",
    ("loops", "type"): "my_action",
    ("loops", "owner"): "me",
    ("loops", "source"): "explicit_commitment",
    ("loops", "confidence"): "high",
    ("learning_proposals", "kind"): "merge",
    ("followup_decisions", "stage"): "rules",
    ("embeddings", "dim"): 1,
    ("embeddings", "vector"): b"\x00\x00\x00\x00",
}
_CHECK = re.compile(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)", re.S | re.I)


def _check_values() -> dict:
    """{(table, column): first allowed value} from every CHECK(col IN (...)) in the tracked schema."""
    out = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", catalog.schema_sql(), re.S):
        table, body = m.group(1), m.group(2)
        for cm in _CHECK.finditer(body):
            values = re.findall(r"'([^']*)'", cm.group(2))
            if values:
                out.setdefault((table, cm.group(1)), values[0])
    return out


CHECKS = _check_values()


class Owner:
    """The parent rows one owner needs, made on demand as that owner (the connection must be bound
    to them when a parent is first made) and remembered for the test."""

    def __init__(self, conn, user_id: str):
        self.conn = conn
        self.user_id = user_id
        self.rows: dict = {}                       # parent table -> locator of the one row made for it
        self.directory: dict = {}                  # 'person' | 'account' -> node id

    def parent(self, table: str):
        if table not in self.rows:
            self.rows[table] = insert(self.conn, table, self.user_id, self)
        return self.rows[table]

    def node(self, kind: str) -> str:
        if kind in DIRECTORY.values():
            if kind not in self.directory:
                if kind == "person":
                    self.directory[kind] = repo.create_person(self.conn, uniq("Person"), email=f"{uniq('p')}@acme.test")
                else:
                    self.directory[kind] = repo.create_account(self.conn, uniq("Acme"), [f"{uniq('d')}.test"])
            return self.directory[kind]
        node_id = uniq(f"{kind}")
        self.conn.execute("INSERT INTO nodes(id,type,title,owner_id) VALUES (?,?,?,?)",
                          (node_id, kind, f"{kind} of {self.user_id}", self.user_id))
        return node_id


def _value(table: str, col: catalog.Column, owner: Owner, owner_id: str):
    name = col.name
    if name == "owner_id":
        return owner_id
    if name == "node_id" and table in NODE_TYPES:
        return owner.node(NODE_TYPES[table])
    if name in DIRECTORY:
        return owner.node(DIRECTORY[name])
    if name in PARENTS:
        parent, key = PARENTS[name]
        if parent == "nodes":
            return owner.node("call")
        return owner.parent(parent)[key]
    if (table, name) in VALUES:
        return VALUES[(table, name)]
    if (table, name) in CHECKS:
        return CHECKS[(table, name)]
    if col.type == "TEXT":
        return uniq(f"{table}.{name}")
    if col.type == "INTEGER":
        return next(_seq) if col.pk else 1
    if col.type == "REAL":
        return 1.0
    if col.type == "BLOB":
        return b"\x00"
    raise NoFactory(f"{table}.{name}: no rule for type {col.type}; add it to tests/isolation/factories.VALUES")


def columns_to_fill(table: str) -> list:
    """The columns a minimal INSERT must name: NOT NULL without a default, plus owner_id and every parent
    column (so that a child row hangs on a real parent even when the column is nullable)."""
    out = []
    for col in catalog.tables()[table]:
        identity_pk = col.pk and col.name == "id" and col.type == "INTEGER"
        needed = (col.notnull or col.pk) and col.default is None and not identity_pk
        if needed or col.name == "owner_id" or col.name in PARENTS or col.name in DIRECTORY:
            out.append(col)
    return out


def insert(conn, table: str, owner_id: str, owner: Owner) -> dict:
    """Insert one row of `table` owned by `owner_id`, its parents from `owner`. Returns the locator."""
    if tenancy.TABLE_CLASS.get(table) != tenancy.OWNED:
        raise NoFactory(f"{table} is not OWNED")
    cols = columns_to_fill(table)
    values = {c.name: _value(table, c, owner, owner_id) for c in cols}
    if table == "learned_patterns":
        values["id"] = f"lp:seller:u:{owner_id}:{uniq('k')}"
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table}({names}) VALUES ({marks})", tuple(values.values()))
    pk = [c for c in catalog.tables()[table] if c.pk]
    if not pk:
        raise NoFactory(f"{table} has no primary key; the matrix cannot locate its rows")
    locator = {}
    for c in sorted(pk, key=lambda c: c.pk):
        if c.name == "id" and c.type == "INTEGER" and c.name not in values:
            locator["id"] = db.insert_id(cur)
        else:
            locator[c.name] = values[c.name]
    return locator


def where(locator: dict) -> tuple:
    """('col1=? AND col2=?', (v1, v2)) for a locator."""
    return " AND ".join(f"{k}=?" for k in locator), tuple(locator.values())


def count(conn, table: str, locator: dict) -> int:
    sql, params = where(locator)
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {sql}", params).fetchone()[0]


def touch(conn, table: str, locator: dict) -> int:
    """An UPDATE that changes nothing; its rowcount says how many rows the actor may write."""
    sql, params = where(locator)
    return conn.execute(f"UPDATE {table} SET owner_id = owner_id WHERE {sql}", params).rowcount


def delete(conn, table: str, locator: dict) -> int:
    sql, params = where(locator)
    return conn.execute(f"DELETE FROM {table} WHERE {sql}", params).rowcount


def stamp() -> str:
    return now()
