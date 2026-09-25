"""Who may change what is on screen.

One rule, applied twice:

  * RENDERING. A page shows one object (a call, a deal, an email) or one person's coaching. When the
    acting user is not its owner, the page renders READ ONLY: every form and button that would write is
    left out, and a banner says whose it is. Comments are the one thing a reader may add (store/rls.py,
    OWNED_EXCEPTIONS). `readonly(conn, owner_id)` is the whole decision, made from the row's owner_id.
  * WRITING. Every non-GET request runs inside write_request() (web/app.ActorGate). A route that looks up
    the object it is about to change through guard() (every *_or_404 helper does) is refused with
    ReadOnly (403) when the acting user is not the owner, before anything is written or queued. The
    row-level policies refuse the write anyway (a manager SELECTs a rep's rows and can write none); this
    makes the refusal a clear 403 instead of a silent no-op, and covers the one shared table the policies
    leave open, the bus (a manager must not queue a redraft of a rep's email).

The team: a manager reads the rep rows of every team they manage (team_managers + users.team_id), which
is exactly app_visible_owners() on Postgres. managed_reps() is the same list in Python, for the pages.
An admin who manages no team manages nobody and sees no content (and no Team page).
"""
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from .. import identity

_writing: ContextVar = ContextVar("salescoach_write_request", default=False)


class ReadOnly(PermissionError):
    """A write on something the acting user can read but does not own. The web layer answers 403."""

    def __init__(self, owner_id: Optional[str] = None, what: str = "this"):
        self.owner_id = owner_id
        super().__init__(f"Read only: {what} belongs to someone else. You can read it and comment; only its "
                         f"owner can change it.")


@contextmanager
def write_request(on: bool = True):
    """Mark the block as a state-changing request (ActorGate does it for every non-GET)."""
    token = _writing.set(bool(on))
    try:
        yield
    finally:
        _writing.reset(token)


def writing() -> bool:
    return _writing.get()


def actor_id(conn=None) -> str:
    return (identity.actor_of(conn) if conn is not None else identity.current_actor()).user_id


def is_own(conn, owner_id) -> bool:
    """The acting user owns it. A NULL owner is the org directory (accounts, people): anyone's to edit."""
    return owner_id is None or owner_id == actor_id(conn)


def readonly(conn, owner_id) -> bool:
    """Render read-only: the viewer is not the owner."""
    return not is_own(conn, owner_id)


def _owner(row):
    if row is None:
        return None, False
    try:
        return row["owner_id"], True
    except (KeyError, IndexError):
        return None, False


def guard(conn, row, what: str = "this"):
    """In a write request, refuse a row the acting user does not own (ReadOnly). Returns the row."""
    owner, has = _owner(row)
    if has and writing() and not is_own(conn, owner):
        raise ReadOnly(owner, what)
    return row


def require_own(conn, owner_id, what: str = "this") -> None:
    """Refuse outright (in any request) when the acting user does not own `owner_id`'s object."""
    if not is_own(conn, owner_id):
        raise ReadOnly(owner_id, what)


# ---- the team -----------------------------------------------------------------------------------------

def managed_team_ids(conn, user_id: Optional[str] = None) -> list:
    """The teams the user manages: their team_managers rows, while their role is manager or admin (the same
    rule as app_visible_owners(): a manager demoted to rep manages nothing, whatever rows were left behind)."""
    user_id = user_id or actor_id(conn)
    return [r[0] for r in conn.execute(
        "SELECT tm.team_id FROM team_managers tm JOIN users u ON u.id = tm.user_id "
        "WHERE tm.user_id=? AND u.role IN ('manager', 'admin') ORDER BY tm.team_id", (user_id,)).fetchall()]


def manages_team(conn, user_id: Optional[str] = None) -> bool:
    """Shows the Team page and nav entry. A local (single-user) install has no teams."""
    if not identity.cloud():
        return False
    return bool(managed_team_ids(conn, user_id))


def managed_reps(conn, user_id: Optional[str] = None) -> list:
    """Every member of every team the user manages, themselves excluded, disabled users last: the owners
    app_visible_owners() adds to the user's own id. [{id, name, email, team_id, team_name, status, role}]"""
    user_id = user_id or actor_id(conn)
    teams = managed_team_ids(conn, user_id)
    if not teams:
        return []
    marks = ",".join("?" * len(teams))
    rows = conn.execute(
        f"SELECT u.id, u.name, u.email, u.team_id, u.status, u.role, t.name AS team_name FROM users u "
        f"LEFT JOIN teams t ON t.id=u.team_id WHERE u.team_id IN ({marks}) AND u.id<>? "
        f"ORDER BY u.status='disabled', t.name, u.name, u.id", (*teams, user_id)).fetchall()
    return [dict(r) for r in rows]


def visible_owner_ids(conn) -> list:
    """The acting user, then the reps they manage: whose rows the policies let them read."""
    me = actor_id(conn)
    return [me] + [r["id"] for r in managed_reps(conn, me)]


def can_view(conn, user_id: str) -> bool:
    return user_id in visible_owner_ids(conn)


def user_name(conn, user_id: Optional[str]) -> str:
    if not user_id:
        return ""
    if user_id == identity.LOCAL_USER and not identity.cloud():
        from .. import seller
        return seller.profile().get("name") or "you"
    row = conn.execute("SELECT name, email FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return user_id
    return row["name"] or row["email"] or user_id


def names(conn, user_ids) -> dict:
    ids = [u for u in dict.fromkeys(user_ids) if u]
    if not ids:
        return {}
    rows = conn.execute(f"SELECT id, name, email FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    out = {r["id"]: (r["name"] or r["email"] or r["id"]) for r in rows}
    return {u: out.get(u) or user_name(conn, u) for u in ids}


def page_owner(conn, owner_id) -> dict:
    """What a template needs to render someone's object: {readonly, owner_id, owner_name}."""
    ro = readonly(conn, owner_id)
    return {"readonly": ro, "owner_id": owner_id, "owner_name": user_name(conn, owner_id) if ro else ""}


@contextmanager
def viewing(conn, user_id: Optional[str]):
    """Render a person's own-work page (Coach, Learning) for `user_id`: themselves, or a rep the acting user
    manages. Anyone else is simply not there (LookupError, which the routes answer 404), like a hidden row."""
    if user_id and user_id != actor_id(conn):
        if not can_view(conn, user_id):
            raise LookupError(user_id)
        with identity.viewing(user_id):
            yield user_id
        return
    with identity.viewing(None):
        yield actor_id(conn)


# ---- the write guard by path (web/app.WriteGuard) --------------------------------------------------------
# A write request whose path names an object (every POST under /calls/<id>/, /deals/<id>/, /loops/<id>/, ...)
# is checked BEFORE its route runs: the object is looked up as the acting user, and one they can read but do
# not own is refused (ReadOnly, 403). One check in front of every route, the ones added by later phases
# included, so a new write route on a rep's object cannot forget it. The *_or_404 helpers check again.
_EMAIL = "SELECT owner_id FROM emails WHERE id=?"
WRITE_PATHS = (
    (re.compile(r"^/calls/([^/]+)/"), "SELECT owner_id FROM calls WHERE node_id=?", str),
    (re.compile(r"^/live/([^/]+)/"), "SELECT owner_id FROM calls WHERE node_id=?", str),
    (re.compile(r"^/coach/(?:replay|live)/([^/]+)"), "SELECT owner_id FROM calls WHERE node_id=?", str),
    (re.compile(r"^/deals/([^/]+)/"), "SELECT owner_id FROM deals WHERE node_id=?", str),
    (re.compile(r"^/(?:loops|followups)/([^/]+)/"), "SELECT owner_id FROM loops WHERE node_id=?", str),
    (re.compile(r"^/(?:emails|nudges)/(\d{1,9})/"), _EMAIL, int),
    (re.compile(r"^/replies/(\d{1,9})/"), "SELECT owner_id FROM email_replies WHERE id=?", int),
    (re.compile(r"^/conflicts/(\d{1,9})/"), "SELECT owner_id FROM memory_conflicts WHERE id=?", int),
    (re.compile(r"^/learning/proposals/(\d{1,9})/"), "SELECT owner_id FROM learning_proposals WHERE id=?", int),
)


def path_owner(conn, path: str):
    """(found, owner_id) of the object a write path names, read as the acting user; (False, None) when the
    path names none or the object is not there for them (the route then answers 404 as always)."""
    for pattern, sql, cast in WRITE_PATHS:
        m = pattern.match(path or "")
        if m:
            row = conn.execute(sql, (cast(m.group(1)),)).fetchone()
            return (row is not None, row["owner_id"] if row is not None else None)
    return False, None


def refuse_write(conn, path: str) -> None:
    """ReadOnly when a write path names an object the acting user can read but does not own."""
    found, owner = path_owner(conn, path)
    if found and not is_own(conn, owner):
        raise ReadOnly(owner)
