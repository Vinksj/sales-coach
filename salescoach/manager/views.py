"""The access log: who opened someone else's work, and when.

Written when a user who is not the owner reads a rep's work (in practice, a manager), at most once per
viewer and object every THROTTLE_S (a page that refreshes itself while a call is processing would otherwise
log every five seconds). Insert-only (store/rls.py, access_log): the owner and the owner's managers read it;
nobody edits or deletes a row. The rep sees "Viewed by" on the call, deal, Coach and Learning pages, so
reading a rep's work is never invisible to the rep.

Which reads are logged is decided in ONE place, by path, not by each route: READ_PATHS (a GET under
/calls/<id>, /live/<id>, /coach/live/<id>, /deals/<id>, /runs/<id>, /nudges/<id>, any sub-path included) and
REP_PAGES (/coach, /learning, /coach/intel with ?rep=). web/app.ReadLog applies it to every successful GET
in cloud mode, so a route added under those paths is logged without asking for it, and
tests/isolation/test_access_log_inventory.py fails for a new GET route that names an object or a rep and is
neither covered here nor listed there as exempt with a reason. What each kind names:

  call      a call and everything read off it (runs, clip, live page, nudge timeline and JSON)
  deal      a deal and its fragments (intelligence, prep, outcome)
  email     a follow-up nudge email (/nudges/<id>)
  coaching  a person's own-work pages (Coach, Learning, coach intelligence): entity_id is the rep's user id

An agent run is logged as its call (or, for a deal-level run, its deal).
"""
import json
import re
from datetime import datetime, timezone
from typing import Optional

from ..store.stores import now
from . import access

THROTTLE_S = 600
KINDS = ("call", "deal", "email", "coaching")


def _call(conn, call_id):
    row = conn.execute("SELECT node_id, owner_id FROM calls WHERE node_id=?", (call_id,)).fetchone()
    return ("call", row["node_id"], row["owner_id"]) if row else None


def _deal(conn, deal_id):
    row = conn.execute("SELECT node_id, owner_id FROM deals WHERE node_id=?", (deal_id,)).fetchone()
    return ("deal", row["node_id"], row["owner_id"]) if row else None


def _email(conn, email_id):
    row = conn.execute("SELECT id, owner_id FROM emails WHERE id=?", (int(email_id),)).fetchone()
    return ("email", str(row["id"]), row["owner_id"]) if row else None


def _run(conn, run_id):
    row = conn.execute("SELECT call_id, input_refs, owner_id FROM agent_runs WHERE id=?", (int(run_id),)).fetchone()
    if row is None:
        return None
    if row["call_id"]:
        return _call(conn, row["call_id"])
    try:
        deal_id = (json.loads(row["input_refs"] or "{}") or {}).get("deal_id")
    except (ValueError, AttributeError):
        deal_id = None
    return _deal(conn, deal_id) if deal_id else None


# A GET whose path starts with one of these reads the object the first group names (sub-paths included).
READ_PATHS = (
    (re.compile(r"^/calls/([^/]+)(?:/|$)"), _call),
    (re.compile(r"^/live/([^/]+)(?:/|$)"), _call),
    (re.compile(r"^/coach/live/([^/]+)(?:/|$)"), _call),
    (re.compile(r"^/deals/([^/]+)(?:/|$)"), _deal),
    (re.compile(r"^/runs/(\d{1,9})(?:/|$)"), _run),
    (re.compile(r"^/nudges/(\d{1,9})(?:/|$)"), _email),
)
# A GET of one of these with ?rep=<user id> reads that person's coaching.
REP_PAGES = ("/coach", "/learning", "/coach/intel")


def logs(path: str, rep: Optional[str] = None) -> bool:
    """Whether a GET of `path` (with ?rep=`rep`) is one the access log is about. No database."""
    return bool(rep and path in REP_PAGES) or any(p.match(path or "") for p, _ in READ_PATHS)


def read_entity(conn, path: str, rep: Optional[str] = None):
    """(kind, entity_id, owner_id) of the work a GET reads, looked up as the acting user; None when the path
    names nothing logged or the object is not there for them (the route answers 404 anyway)."""
    if rep and path in REP_PAGES:
        return ("coaching", rep, rep) if access.can_view(conn, rep) else None
    for pattern, resolve in READ_PATHS:
        m = pattern.match(path or "")
        if m:
            try:
                return resolve(conn, m.group(1))
            except ValueError:
                return None
    return None


def log_read(conn, path: str, rep: Optional[str] = None) -> bool:
    """Log the acting user's read of someone else's work named by a GET path. Commits when it logs."""
    found = read_entity(conn, path, rep)
    return log_view(conn, *found) if found else False


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
