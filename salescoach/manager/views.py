"""The access log: who opened someone else's call or deal page, and when.

Written when a user who is not the owner opens a call or deal page (in practice, a manager reading a
rep's work), at most once per viewer and object every THROTTLE_S (a page that refreshes itself while a
call is processing would otherwise log every five seconds). Insert-only (store/rls.py, access_log): the
owner and the owner's managers read it; nobody edits or deletes a row. The rep sees "Viewed by" on the
page, so reading a rep's work is never invisible to the rep.
"""
from datetime import datetime, timezone

from ..store.stores import now
from . import access

THROTTLE_S = 600
KINDS = ("call", "deal")


def _age_s(stamp) -> float:
    try:
        ts = datetime.fromisoformat(str(stamp))
    except ValueError:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def log_view(conn, entity_type: str, entity_id: str, owner_id) -> bool:
    """Record that the acting user opened `owner_id`'s object, unless it is their own. Commits."""
    viewer = access.actor_id(conn)
    if entity_type not in KINDS or not owner_id or owner_id == viewer:
        return False
    last = conn.execute("SELECT viewed_at FROM access_log WHERE viewer_id=? AND entity_type=? AND entity_id=? "
                        "ORDER BY id DESC LIMIT 1", (viewer, entity_type, entity_id)).fetchone()
    if last is not None and _age_s(last["viewed_at"]) < THROTTLE_S:
        return False
    conn.execute("INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) VALUES (?,?,?,?,?)",
                 (viewer, owner_id, entity_type, entity_id, now()))
    conn.commit()
    return True


def viewed_by(conn, entity_type: str, entity_id: str) -> list:
    """[{viewer_id, name, last, n}] most recent first: the "Viewed by" line."""
    rows = conn.execute(
        "SELECT viewer_id, MAX(viewed_at) AS last, COUNT(*) AS n FROM access_log WHERE entity_type=? AND entity_id=? "
        "GROUP BY viewer_id ORDER BY MAX(viewed_at) DESC", (entity_type, entity_id)).fetchall()
    who = access.names(conn, [r["viewer_id"] for r in rows])
    return [{"viewer_id": r["viewer_id"], "name": who.get(r["viewer_id"], r["viewer_id"]), "last": r["last"],
             "n": r["n"]} for r in rows]
