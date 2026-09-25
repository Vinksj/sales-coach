"""The manager routes, mounted by plugins/manager.py.

  GET  /team                       the team dashboard (managers; an admin who manages no team gets a note
                                   pointing at team setup; everyone else 404; a local install 404s with
                                   "team features need the cloud install")
  GET  /calls                      every call the viewer may read, filtered by rep, dates, deal, state,
                                   methodology gap or follow-up email (row-level security scopes it)
  POST /comments                   entity_type, entity_id, body, [turn_idx], [next]: a comment on a call,
                                   a moment of a call, a deal, an email or a loop; entity_type coaching
                                   with entity_id <rep id> is a coaching note on the rep's Learning page
  POST /comments/{id}/resolve      the rep whose comment it is (or its author)
  POST /comments/{id}/delete       its author
Every POST passes the same-origin guard like any other. Nothing here calls a model.
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import identity
from . import access, calls, comments, team

router = APIRouter()
CLOUD_ONLY = "Team features need the cloud install."


def _web():
    from ..web import app as webapp
    return webapp


def _methodology():
    try:
        from ..intel import methodology
        return methodology.active()
    except Exception:
        return None


# ---- the team ------------------------------------------------------------------------------------------

@router.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    webapp = _web()
    if not identity.cloud():
        raise HTTPException(404, CLOUD_ONLY)
    with webapp._db(request) as conn:
        actor = identity.actor_of(conn)
        if not access.manages_team(conn):
            if actor.role != "admin":
                raise HTTPException(404, "Not found")
            return webapp.render(request, conn, "team.html", no_team=True, reps=[], rollup=None, windows={},
                                 methodology="")
        data = team.dashboard(conn, webapp.today_ist())
        return webapp.render(request, conn, "team.html", no_team=False, **data)


# ---- the calls index -----------------------------------------------------------------------------------

@router.get("/calls", response_class=HTMLResponse)
def calls_index(request: Request):
    webapp = _web()
    m = _methodology()
    f = calls.clean_filters(request.query_params, m.keys if m else ())
    with webapp._db(request) as conn:
        me = access.actor_id(conn)
        reps = access.managed_reps(conn)
        rows = calls.query(conn, f)
        who = access.names(conn, [me] + [r["owner_id"] for r in rows])
        counts = comments.counts(conn, "call", [r["node_id"] for r in rows])
        for r in rows:
            r["owner_name"] = who.get(r["owner_id"], r["owner_id"])
            r["state_label"] = calls.state_label(r)
            r["comments"], r["open_comments"] = counts.get(r["node_id"], (0, 0))
        deals = conn.execute("SELECT node_id, name, owner_id FROM deals ORDER BY name, node_id").fetchall()
        return webapp.render(request, conn, "calls_index.html", rows=rows, f=f, reps=reps, me=me,
                             me_name=who.get(me, me), deals=deals, states=calls.STATES, emails=calls.EMAIL,
                             gaps=[(k, m.label(k)) for k in m.keys] if m else [], limit=calls.LIMIT,
                             show_owner=bool(reps), rep_name=who.get(f["rep"]) or access.user_name(conn, f["rep"]))


# ---- comments ---------------------------------------------------------------------------------------------

@router.post("/comments")
def comment_add(request: Request, entity_type: str = Form(""), entity_id: str = Form(""), body: str = Form(""),
                turn_idx: str = Form(""), next_url: str = Form("", alias="next")):
    webapp = _web()
    with webapp._db(request) as conn:
        default = comments.href(conn, {"entity_type": entity_type, "entity_id": entity_id,
                                       "turn_idx": int(turn_idx) if turn_idx.isdigit() else None}) \
            if entity_type in comments.ENTITY_TYPES and entity_id else "/"
        back = webapp._clean_next(next_url, default)
        try:
            comment_id = comments.add(conn, entity_type, entity_id, body, turn_idx or None)
        except LookupError:
            conn.rollback()
            raise HTTPException(404, "Not found")
        except comments.CommentError as exc:
            conn.rollback()
            return webapp._redirect(back, err=str(exc))
        conn.commit()
    what = "Coaching note saved." if entity_type == "coaching" else "Comment added."
    return webapp._redirect(back, msg=what, anchor=f"c-{comment_id}")


def _comment_action(request: Request, comment_id: int, action, msg: str, next_url: str):
    webapp = _web()
    with webapp._db(request) as conn:
        row = comments.get(conn, comment_id)
        if row is None:
            raise HTTPException(404, "Not found")
        back = webapp._clean_next(next_url, comments.href(conn, row))
        try:
            done = action(conn, comment_id)
        except LookupError:
            conn.rollback()
            raise HTTPException(404, "Not found")
        conn.commit()
    return webapp._redirect(back, msg=msg if done is not False else "Already resolved.")


@router.post("/comments/{comment_id}/resolve")
def comment_resolve(request: Request, comment_id: int, next_url: str = Form("", alias="next")):
    return _comment_action(request, comment_id, comments.resolve, "Resolved.", next_url)


@router.post("/comments/{comment_id}/delete")
def comment_delete(request: Request, comment_id: int, next_url: str = Form("", alias="next")):
    return _comment_action(request, comment_id, comments.delete, "Comment deleted.", next_url)
