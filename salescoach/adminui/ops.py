"""What an admin can change, as plain functions on a connection: each one writes its rows and an
audit event (users.audit: kind admin.*, actor_user_id = the admin) in the same transaction, and
the routes in adminui/web.py only parse forms and call these.

An invite is an allow-list entry: a users row with status 'invited' (so role and team are set
before the person ever signs in) plus an invites row saying who added it and when it was taken up.
The app sends no email. Disabling a user revokes every session and every OAuth grant at once;
enabling them again puts the row back to 'active' (or 'invited' if they never signed in) and
leaves them to sign in and reconnect Google themselves.
"""
from typing import Optional

from .. import identity, sessions, users
from ..execution import tokens
from ..store.stores import now


class AdminError(ValueError):
    pass


def _me(conn) -> str:
    return identity.actor_of(conn).user_id


def _norm(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email or " " in email or email.count("@") != 1 or not email.rsplit("@", 1)[1]:
        raise AdminError("that is not an email address")
    return email


def invite(conn, email: str, role: str = "rep", team_id: Optional[str] = None, name: str = "") -> dict:
    email = _norm(email)
    if role not in users.ROLES:
        raise AdminError(f"role must be one of {', '.join(users.ROLES)}")
    if team_id and users.get_team(conn, team_id) is None:
        raise AdminError("no such team")
    if users.by_email(conn, email) is not None:
        raise AdminError(f"{email} is already on the list")
    try:
        row = users.create(conn, email, name, role=role, team_id=team_id or None, status="invited")
    except users.UserError as exc:
        raise AdminError(str(exc)) from exc
    conn.execute("INSERT INTO invites(email,user_id,invited_by,created_at,accepted_at) VALUES (?,?,?,?,NULL)",
                 (email, row["id"], _me(conn), now()))
    users.audit(conn, "admin.invite", {"user_id": row["id"], "email": email, "role": role, "team_id": team_id or None})
    return row


def update_user(conn, user_id: str, role: Optional[str] = None, team_id: Optional[str] = None) -> dict:
    row = users.get(conn, user_id)
    if row is None:
        raise AdminError("no such user")
    fields = {}
    if role is not None and role != row["role"]:
        if role not in users.ROLES:
            raise AdminError(f"role must be one of {', '.join(users.ROLES)}")
        if user_id == _me(conn):
            raise AdminError("you cannot change your own role; ask another admin")
        fields["role"] = role
    team_id = team_id or None
    if team_id != row["team_id"]:
        if team_id and users.get_team(conn, team_id) is None:
            raise AdminError("no such team")
        fields["team_id"] = team_id
    if not fields:
        return row
    after = users.update(conn, user_id, **fields)
    users.audit(conn, "admin.user.update", {"user_id": user_id, **fields},
                before={k: row[k] for k in fields})
    return after


def disable(conn, user_id: str) -> dict:
    row = users.get(conn, user_id)
    if row is None:
        raise AdminError("no such user")
    if user_id == _me(conn):
        raise AdminError("you cannot disable yourself; ask another admin")
    if row["status"] == "disabled":
        return row
    after = users.update(conn, user_id, status="disabled")
    ended = sessions.revoke_all(conn, user_id)
    revoked = tokens.revoke_all_for_user(conn, user_id)
    users.audit(conn, "admin.user.disable", {"user_id": user_id, "sessions_revoked": ended, "grants_revoked": revoked},
                before={"status": row["status"]})
    return after


def enable(conn, user_id: str) -> dict:
    row = users.get(conn, user_id)
    if row is None:
        raise AdminError("no such user")
    if row["status"] != "disabled":
        return row
    status = "active" if row["google_sub"] else "invited"
    after = users.update(conn, user_id, status=status)
    users.audit(conn, "admin.user.enable", {"user_id": user_id, "status": status}, before={"status": "disabled"})
    return after


def logout_everywhere(conn, user_id: str) -> int:
    if users.get(conn, user_id) is None:
        raise AdminError("no such user")
    ended = sessions.revoke_all(conn, user_id)
    users.audit(conn, "admin.user.logout", {"user_id": user_id, "sessions_revoked": ended})
    return ended


def create_team(conn, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise AdminError("a team needs a name")
    if any(t["name"].lower() == name.lower() for t in users.list_teams(conn)):
        raise AdminError(f"there is already a team called {name}")
    team = users.create_team(conn, name)
    users.audit(conn, "admin.team.create", {"team_id": team["id"], "name": name})
    return team


def rename_team(conn, team_id: str, name: str) -> dict:
    team = users.get_team(conn, team_id)
    if team is None:
        raise AdminError("no such team")
    name = (name or "").strip()
    if not name:
        raise AdminError("a team needs a name")
    if name == team["name"]:
        return team
    conn.execute("UPDATE teams SET name=? WHERE id=?", (name, team_id))
    users.audit(conn, "admin.team.rename", {"team_id": team_id, "name": name}, before={"name": team["name"]})
    return users.get_team(conn, team_id)


def set_managers(conn, team_id: str, user_ids) -> list:
    """Who reads this team's work. Only a manager or an admin can be one: a rep would otherwise read
    colleagues' calls with a rep's role."""
    if users.get_team(conn, team_id) is None:
        raise AdminError("no such team")
    wanted = list(dict.fromkeys(u for u in (user_ids or ()) if u))
    for uid in wanted:
        row = users.get(conn, uid)
        if row is None:
            raise AdminError(f"no such user: {uid}")
        if row["role"] not in ("manager", "admin"):
            raise AdminError(f"{row['email'] or uid} is a rep; make them a manager first")
    before = users.managers_of(conn, team_id)
    if sorted(before) == sorted(wanted):
        return before
    after = users.set_managers(conn, team_id, wanted)
    users.audit(conn, "admin.team.managers", {"team_id": team_id, "managers": after}, before={"managers": before})
    return after


# ---- what the page shows -----------------------------------------------------------------------

def overview(conn) -> dict:
    """Users with their team, last sign-in, live sessions and Google connection; teams with managers."""
    teams = {t["id"]: t for t in users.list_teams(conn)}
    last = {r["user_id"]: r["last"] for r in conn.execute(
        "SELECT user_id, MAX(created_at) AS last FROM sessions GROUP BY user_id").fetchall()}
    live = {r["user_id"]: r["n"] for r in conn.execute(
        "SELECT user_id, COUNT(*) AS n FROM sessions WHERE revoked_at IS NULL AND expires_at > ? GROUP BY user_id",
        (now(),)).fetchall()}
    grants = {r["user_id"]: r for r in conn.execute("SELECT * FROM oauth_tokens WHERE provider='google'").fetchall()}
    invites = {r["email"]: dict(r) for r in conn.execute("SELECT * FROM invites").fetchall()}
    rows = []
    for u in users.list_users(conn):
        grant = grants.get(u["id"])
        status = tokens.status_of(conn, u["id"]) if grant else {"status": "not_connected", "features": []}
        rows.append({**u, "team": teams.get(u["team_id"], {}).get("name") if u["team_id"] else None,
                     "last_sign_in": last.get(u["id"]), "sessions": live.get(u["id"], 0),
                     "google": status["status"], "features": status["features"],
                     "invited_by": (invites.get(u["email"]) or {}).get("invited_by"),
                     "manages": users.teams_managed_by(conn, u["id"])})
    by_id = {u["id"]: u for u in rows}
    team_rows = [{**t, "managers": [by_id.get(m, {"id": m, "name": m, "email": None}) for m in users.managers_of(conn, t["id"])],
                  "members": [u for u in rows if u["team_id"] == t["id"]]} for t in teams.values()]
    return {"users": rows, "teams": team_rows, "roles": users.ROLES,
            "candidates": [u for u in rows if u["role"] in ("manager", "admin") and u["status"] != "disabled"]}
