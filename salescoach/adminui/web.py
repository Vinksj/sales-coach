"""/admin: the routes. Admin role only (anyone else gets 404: the page does not exist for them).

  GET  /admin                           users, teams, invite form
  POST /admin/invite                    email, role, team_id
  POST /admin/users/{id}                role, team_id
  POST /admin/users/{id}/disable        ends every session and Google grant at once
  POST /admin/users/{id}/enable
  POST /admin/users/{id}/logout         log that user out everywhere
  POST /admin/users/{id}/offboard       mode=reassign (to_user_id) | purge, confirm=<their email>: the rep
                                        leaves (lifecycle/offboard.py); disabled, signed out, grants revoked
  POST /admin/teams                     name
  POST /admin/teams/{id}                name (rename)
  POST /admin/teams/{id}/managers       manager ids (multi)
Every write is one adminui/ops call, which audits it. On a SQLite install the page shows the one
user; an invite there is refused (a team needs Postgres), which the page says.
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import identity
from . import ops

router = APIRouter()
PATH = "/admin"


def _web():
    from ..web import app as webapp
    return webapp


def _admin_only():
    actor = identity.current_actor()
    if actor.role != "admin":
        raise HTTPException(status_code=404, detail="Not found")
    return actor


def _page(request: Request, status: int = 200):
    _admin_only()
    webapp = _web()
    with webapp._db(request) as conn:
        response = webapp.render(request, conn, "admin.html", is_cloud=identity.cloud(), **ops.overview(conn))
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _do(request: Request, action, msg, anchor=None):
    """Run one ops call inside a request; commit; back to the page with what happened."""
    _admin_only()
    webapp = _web()
    with webapp._db(request) as conn:
        try:
            result = action(conn)
        except ops.AdminError as exc:
            conn.rollback()
            return webapp._redirect(PATH, err=str(exc), anchor=anchor)
        conn.commit()
    return webapp._redirect(PATH, msg=msg(result) if callable(msg) else msg, anchor=anchor)


@router.get(PATH, response_class=HTMLResponse)
def admin_page(request: Request):
    return _page(request)


@router.post(PATH + "/invite")
def admin_invite(request: Request, email: str = Form(""), role: str = Form("rep"), team_id: str = Form(""),
                 name: str = Form("")):
    return _do(request, lambda conn: ops.invite(conn, email, role, team_id or None, name),
               lambda row: f"{row['email']} can sign in now. Tell them: the coach sends no email.", anchor="users")


@router.post(PATH + "/users/{user_id}")
def admin_user_update(request: Request, user_id: str, role: str = Form(None), team_id: str = Form("")):
    return _do(request, lambda conn: ops.update_user(conn, user_id, role, team_id or None), "Saved.", anchor="users")


@router.post(PATH + "/users/{user_id}/disable")
def admin_user_disable(request: Request, user_id: str):
    return _do(request, lambda conn: ops.disable(conn, user_id),
               lambda row: f"{row['email'] or row['id']} is disabled: signed out everywhere, Google access revoked.",
               anchor="users")


@router.post(PATH + "/users/{user_id}/enable")
def admin_user_enable(request: Request, user_id: str):
    return _do(request, lambda conn: ops.enable(conn, user_id),
               lambda row: f"{row['email'] or row['id']} can sign in again.", anchor="users")


@router.post(PATH + "/users/{user_id}/logout")
def admin_user_logout(request: Request, user_id: str):
    return _do(request, lambda conn: ops.logout_everywhere(conn, user_id),
               lambda n: f"Signed out of {n} browser{'' if n == 1 else 's'}.", anchor="users")


def _offboarded(result: dict) -> str:
    moved = sum(result["counts"].values())
    return (f"Offboarded: {moved} row{'' if moved == 1 else 's'} changed; signed out of {result['sessions_revoked']} "
            f"browser{'' if result['sessions_revoked'] == 1 else 's'}, Google access revoked, user disabled.")


@router.post(PATH + "/users/{user_id}/offboard")
def admin_user_offboard(request: Request, user_id: str, mode: str = Form(""), to_user_id: str = Form(""),
                        confirm: str = Form("")):
    def act(conn):
        from .. import users
        row = users.get(conn, user_id)
        if row is None:
            raise ops.AdminError("no such user")
        if (confirm or "").strip().lower() != (row["email"] or row["id"]).lower():
            raise ops.AdminError("type the person's email address to confirm: offboarding cannot be undone")
        return ops.offboard(conn, user_id, mode, to_user_id or None)
    return _do(request, act, _offboarded, anchor="users")


@router.post(PATH + "/teams")
def admin_team_create(request: Request, name: str = Form("")):
    return _do(request, lambda conn: ops.create_team(conn, name), lambda t: f"Team {t['name']} created.", anchor="teams")


@router.post(PATH + "/teams/{team_id}")
def admin_team_rename(request: Request, team_id: str, name: str = Form("")):
    return _do(request, lambda conn: ops.rename_team(conn, team_id, name), "Renamed.", anchor="teams")


@router.post(PATH + "/teams/{team_id}/managers")
async def admin_team_managers(request: Request, team_id: str):
    form = await request.form()
    ids = form.getlist("manager_id")
    return _do(request, lambda conn: ops.set_managers(conn, team_id, ids), "Managers saved.", anchor="teams")
