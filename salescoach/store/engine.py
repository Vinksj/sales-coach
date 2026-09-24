"""The graph/event engine sales.db is built on.

One rule: every mutation of a node goes through here, and every mutation emits
an event. The tables hold the authoritative current state; `events` holds the
authoritative history, so the audit log can never drift from reality.

This is the subset of the original engine that the coach uses, kept inside the
package so an install has no dependency outside it.
"""
import json
from datetime import datetime, timezone

from . import db


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path) -> db.Connection:
    """A SQLite file (rows by name and index, foreign keys on) behind the store/db.py surface."""
    return db.connect(path)


def init(conn, schema) -> None:
    """Create the schema from a SQL script: SQLite only (Postgres is migrated by store/pgmigrate.py)."""
    with open(schema) as f:
        conn.executescript(f.read())
    conn.commit()


def _emit(conn, actor, kind, node_id=None, edge_id=None, before=None, after=None, source_id=None, ts=None):
    conn.execute(
        "INSERT INTO events(ts, actor, kind, node_id, edge_id, before, after, source_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts or now(), actor, kind, node_id, edge_id,
         json.dumps(before) if before is not None else None,
         json.dumps(after) if after is not None else None,
         source_id),
    )


DIRECTORY_TYPES = ("account", "person")       # the org's shared directory: nodes nobody owns


def add_node(conn, actor, id, type, kind=None, title=None, standfirst=None,
             lenses=None, status=None, confidence=0.5, created_at=None,
             body_ref=None, source_id=None):
    """Owned by whoever the connection acts for, through the column's default (the session setting
    on Postgres, 'local' on SQLite: one seller per file); account and person nodes belong to the org
    directory and get owner_id NULL explicitly (store/tenancy.py, OWNER_NULLABLE)."""
    ts = created_at or now()
    row = dict(id=id, type=type, kind=kind, title=title, standfirst=standfirst,
               lenses=json.dumps(lenses or []), status=status, confidence=confidence,
               created_at=ts, last_reinforced_at=ts, decay_rate=0.0, body_ref=body_ref)
    owner_col, owner_val = (", owner_id", ", NULL") if type in DIRECTORY_TYPES else ("", "")
    conn.execute(
        f"INSERT INTO nodes(id,type,kind,title,standfirst,lenses,status,confidence,"
        f"created_at,last_reinforced_at,decay_rate,body_ref{owner_col}) "
        f"VALUES (:id,:type,:kind,:title,:standfirst,:lenses,:status,:confidence,"
        f":created_at,:last_reinforced_at,:decay_rate,:body_ref{owner_val})", row)
    _emit(conn, actor, "node_created", node_id=id, after=row, source_id=source_id, ts=ts)
    return id


def set_source(conn, actor, node_id, uri=None, sha=None, raw_path=None,
               capture="seed", lineage=None):
    conn.execute(
        "INSERT INTO sources(node_id,uri,sha,raw_path,capture,lineage) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET uri=excluded.uri, sha=excluded.sha, raw_path=excluded.raw_path, "
        "capture=excluded.capture, lineage=excluded.lineage",
        (node_id, uri, sha, raw_path, capture, json.dumps(lineage or [])))
    _emit(conn, actor, "source_registered", node_id=node_id,
          after=dict(uri=uri, capture=capture))


def node_exists(conn, id) -> bool:
    return conn.execute("SELECT 1 FROM nodes WHERE id=?", (id,)).fetchone() is not None
