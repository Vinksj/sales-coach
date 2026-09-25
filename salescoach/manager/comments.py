"""Comments: on a call (the whole call, or one moment of it), a deal, an email or a loop, and coaching
notes a manager leaves on a rep's Learning page.

A comment is OWNED by the rep whose object it is about (owner_id), whoever wrote it (author_id): the rep
reads every comment on their work and resolves it; the rep's managers read the same thread and may add
to it. That is the one deliberate exception to "the owner writes" (store/rls.OWNED_EXCEPTIONS); the
database checks that the author is the acting user and that the owner named IS the commented object's
owner, so this module's checks are the friendly version of a rule that holds without them.

A coaching note is a comment with entity_type 'coaching' and entity_id the rep's user id: written by one
of the rep's managers, never by the rep about themselves, shown on the rep's Learning page.

Nothing here reaches a prompt. The agents build their prompts from calls, deals, loops, emails and the
learned patterns; tests/test_manager.py fails the build if a prompt-building module reads this table.
"""
from typing import Optional

from .. import identity
from ..store.db import insert_id
from ..store.stores import now
from . import access

ENTITY_TYPES = ("call", "deal", "email", "loop", "coaching")
MAX_BODY = 4000


class CommentError(ValueError):
    pass


def entity_owner(conn, entity_type: str, entity_id) -> Optional[str]:
    """The owner of the object a comment would be on, read as the acting user (row-level security): None
    when it does not exist or the acting user may not read it."""
    entity_id = str(entity_id or "").strip()
    if not entity_id:
        return None
    if entity_type == "coaching":
        # About a rep the acting user manages; never about themselves (store/rls.py says the same).
        return entity_id if entity_id in {r["id"] for r in access.managed_reps(conn)} else None
    table, key = {"call": ("calls", "node_id"), "deal": ("deals", "node_id"), "loop": ("loops", "node_id"),
                  "email": ("emails", "id")}.get(entity_type, (None, None))
    if table is None:
        return None
    if entity_type == "email":
        if not entity_id.isdigit() or len(entity_id) > 9:
            return None
        entity_id = int(entity_id)
    row = conn.execute(f"SELECT owner_id FROM {table} WHERE {key}=?", (entity_id,)).fetchone()
    return row["owner_id"] if row else None


def add(conn, entity_type: str, entity_id, body: str, turn_idx=None) -> int:
    """File a comment as the acting user on an object they may read. LookupError when it is not there for
    them; CommentError for an empty or oversized body or a turn the call does not have."""
    if entity_type not in ENTITY_TYPES:
        raise LookupError(entity_type)
    body = (body or "").replace("\r\n", "\n").strip()
    if not body:
        raise CommentError("Write something first.")
    if len(body) > MAX_BODY:
        raise CommentError(f"Keep a comment under {MAX_BODY} characters.")
    owner = entity_owner(conn, entity_type, entity_id)
    if owner is None:
        raise LookupError(f"{entity_type} {entity_id}")
    author = access.actor_id(conn)
    turn = None
    if turn_idx not in (None, ""):
        if entity_type != "call":
            raise CommentError("Only a call has moments to comment on.")
        try:
            turn = int(turn_idx)
        except (TypeError, ValueError):
            raise CommentError("That is not a moment of this call.") from None
        if conn.execute("SELECT 1 FROM turns WHERE call_id=? AND tier='final' AND idx=?",
                        (str(entity_id), turn)).fetchone() is None:
            raise CommentError("That is not a moment of this call.")
    cur = conn.execute(
        "INSERT INTO comments(owner_id,author_id,entity_type,entity_id,turn_idx,body,created_at) VALUES (?,?,?,?,?,?,?)",
        (owner, author, entity_type, str(entity_id), turn, body, now()))
    return insert_id(cur)


def get(conn, comment_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    return dict(row) if row else None


def resolve(conn, comment_id: int) -> bool:
    """The rep whose comment it is (or its author) marks it resolved. ReadOnly for anyone else who can
    read it; LookupError when it is not there for them. False when it was already resolved."""
    row = get(conn, comment_id)
    if row is None:
        raise LookupError(comment_id)
    me = access.actor_id(conn)
    if me not in (row["owner_id"], row["author_id"]):
        raise access.ReadOnly(row["owner_id"], "this comment")
    cur = conn.execute("UPDATE comments SET resolved_at=?, resolved_by=? WHERE id=? AND resolved_at IS NULL",
                       (now(), me, comment_id))
    return cur.rowcount > 0


def delete(conn, comment_id: int) -> None:
    """Its author takes it back."""
    row = get(conn, comment_id)
    if row is None:
        raise LookupError(comment_id)
    if row["author_id"] != access.actor_id(conn):
        raise access.ReadOnly(row["owner_id"], "this comment")
    conn.execute("DELETE FROM comments WHERE id=?", (comment_id,))


def _decorate(conn, rows) -> list:
    out = [dict(r) for r in rows]
    who = access.names(conn, [r["author_id"] for r in out] + [r["resolved_by"] for r in out])
    me = access.actor_id(conn)
    for c in out:
        c["author_name"] = who.get(c["author_id"], c["author_id"])
        c["resolved_by_name"] = who.get(c["resolved_by"], c["resolved_by"]) if c["resolved_by"] else ""
        c["mine"] = c["author_id"] == me
        c["can_resolve"] = not c["resolved_at"] and me in (c["owner_id"], c["author_id"])
    return out


def thread(conn, entity_type: str, entity_id) -> list:
    """Every comment on one object, oldest first (general ones only for a call: see for_call)."""
    return _decorate(conn, conn.execute(
        "SELECT * FROM comments WHERE entity_type=? AND entity_id=? ORDER BY created_at, id",
        (entity_type, str(entity_id))).fetchall())


def for_call(conn, call_id: str) -> dict:
    """{"general": [...], "by_turn": {idx: [...]}, "open": n} for the call page."""
    general, by_turn = [], {}
    for c in thread(conn, "call", call_id):
        if c["turn_idx"] is None:
            general.append(c)
        else:
            by_turn.setdefault(c["turn_idx"], []).append(c)
    everything = general + [c for cs in by_turn.values() for c in cs]
    return {"general": general, "by_turn": by_turn, "open": sum(1 for c in everything if not c["resolved_at"]),
            "n": len(everything)}


def counts(conn, entity_type: str, entity_ids) -> dict:
    """{entity_id: (all, unresolved)} for a list page."""
    ids = [str(i) for i in entity_ids]
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT entity_id, COUNT(*) AS n, SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS open "
        f"FROM comments WHERE entity_type=? AND entity_id IN ({','.join('?' * len(ids))}) GROUP BY entity_id",
        (entity_type, *ids)).fetchall()
    return {r["entity_id"]: (r["n"], r["open"] or 0) for r in rows}


def href(conn, c: dict) -> str:
    """Where a comment lives on screen."""
    kind, eid = c["entity_type"], c["entity_id"]
    if kind == "call":
        return f"/calls/{eid}#" + (f"t{c['turn_idx']}" if c.get("turn_idx") is not None else "comments")
    if kind == "deal":
        return f"/deals/{eid}#comments"
    if kind == "coaching":
        return "/learning#coaching"
    if kind == "email":
        row = conn.execute("SELECT call_id, kind FROM emails WHERE id=?", (int(eid),)).fetchone() if eid.isdigit() else None
        if row is None:
            return "/"
        return f"/nudges/{eid}#comments" if row["kind"] == "nudge" or not row["call_id"] else f"/calls/{row['call_id']}#email"
    if kind == "loop":
        row = conn.execute("SELECT call_id FROM loops WHERE node_id=?", (eid,)).fetchone()
        return f"/calls/{row['call_id']}#loop-{eid}" if row and row["call_id"] else "/loops"
    return "/"


WHAT = {"call": "call", "deal": "deal", "email": "email", "loop": "loop", "coaching": "coaching note"}


def inbox(conn, limit: int = 20) -> list:
    """For the rep's Today: unresolved comments others left on the rep's work, grouped by author.
    [{"author_id", "author_name", "n", "items": [{"href", "what", "label", "created_at", "body"}]}]"""
    me = access.actor_id(conn)
    rows = conn.execute(
        "SELECT * FROM comments WHERE owner_id=? AND author_id<>? AND resolved_at IS NULL "
        "ORDER BY created_at DESC, id DESC LIMIT ?", (me, me, max(1, int(limit)))).fetchall()
    groups: dict = {}
    titles = _titles(conn, rows)
    for c in _decorate(conn, rows):
        g = groups.setdefault(c["author_id"], {"author_id": c["author_id"], "author_name": c["author_name"],
                                               "n": 0, "items": []})
        g["n"] += 1
        g["items"].append({"href": href(conn, c), "what": WHAT.get(c["entity_type"], c["entity_type"]),
                           "label": titles.get((c["entity_type"], c["entity_id"])) or "",
                           "created_at": c["created_at"], "body": c["body"], "turn_idx": c["turn_idx"]})
    return sorted(groups.values(), key=lambda g: -g["n"])


def _titles(conn, rows) -> dict:
    out = {}
    for kind, table, key, col in (("call", "calls", "node_id", "title"), ("deal", "deals", "node_id", "name"),
                                  ("loop", "loops", "node_id", "description")):
        ids = list({r["entity_id"] for r in rows if r["entity_type"] == kind})
        if ids:
            for r in conn.execute(f"SELECT {key} AS k, {col} AS t FROM {table} WHERE {key} IN ({','.join('?' * len(ids))})",
                                  ids).fetchall():
                out[(kind, r["k"])] = r["t"]
    return out


def coaching_notes(conn, rep_id: Optional[str] = None) -> list:
    """The coaching notes about a rep (default: the acting user), newest first."""
    rep_id = rep_id or identity.subject_id(conn)
    return list(reversed(thread(conn, "coaching", rep_id)))
