"""Users, teams and the per-user facts that used to be org-wide.

  users                one row per person who signs in; role rep | manager | admin; status invited |
                       active | disabled. The USER half of the old seller.yaml lives here (name,
                       addresses, aliases, signature, timezone, languages, role title, style guide,
                       call context); the ORG half stays in seller.yaml (seller.ORG_FIELDS).
  teams, team_managers a manager reads a team's work (Phase 2 policies); an admin runs the org.
  user_state           what `state` held for ONE user (automation bookkeeping, dismissed cards);
                       stores.get_user_state / set_user_state key it on the acting user.
  user_speaker_labels  the speaker labels a user said were theirs (was sources.yaml me_labels).

The local install has exactly one user, "local" (identity.LOCAL_USER), created from seller.yaml
the first time the store opens and kept in step by sync_local(). Its profile is still READ from
seller.yaml / style.md, so the single-user product is unchanged; the row exists so that every
per-user table has an owner to point at. A second user is refused on SQLite: multi-user is
Postgres-only (plan, Approach 2).
"""
import json
import uuid
from typing import Optional

from . import identity
from .store.stores import now

ROLES = ("rep", "manager", "admin")
STATUSES = ("invited", "active", "disabled")
LIST_COLUMNS = ("extra_emails", "aliases", "languages")
PROFILE_COLUMNS = ("name", "email", "extra_emails", "aliases", "signature", "timezone", "languages", "role_title",
                   "style", "call_context")
COLUMNS = ("id", "email", "name", "role", "team_id", "status", "google_sub", *LIST_COLUMNS[:1], "aliases", "signature",
           "timezone", "languages", "role_title", "style", "call_context", "created_at", "updated_at")


class UserError(ValueError):
    pass


def new_id() -> str:
    return "u-" + uuid.uuid4().hex[:12]


def _norm_email(email) -> Optional[str]:
    email = (email or "").strip().lower()
    return email or None


def _dump(value) -> str:
    if isinstance(value, str):
        value = [v.strip() for v in value.replace(";", ",").split(",")]
    return json.dumps([str(v).strip() for v in (value or ()) if str(v).strip()])


def _row(row) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    for col in LIST_COLUMNS:
        try:
            out[col] = json.loads(out.get(col) or "[]")
        except ValueError:
            out[col] = []
    return out


def profile_of(row) -> dict:
    """The user's seller.USER_FIELDS from a users row (dict or Row), the shape seller.profile() merges."""
    r = _row(row) if not isinstance(row, dict) else row
    emails = [e for e in [r.get("email"), *(r.get("extra_emails") or [])] if e]
    return {"name": r.get("name") or "", "emails": emails, "aliases": list(r.get("aliases") or []),
            "signature": r.get("signature") or "", "timezone": r.get("timezone") or "",
            "languages": list(r.get("languages") or []), "role_title": r.get("role_title") or "",
            "call_context": r.get("call_context") or "", "style": r.get("style") or ""}


# ---- users ----------------------------------------------------------------------------------------

def get(conn, user_id: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def by_email(conn, email: str) -> Optional[dict]:
    email = _norm_email(email)
    return _row(conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()) if email else None


def list_users(conn, status: Optional[str] = None) -> list:
    if status:
        rows = conn.execute("SELECT * FROM users WHERE status=? ORDER BY name, id", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM users ORDER BY name, id").fetchall()
    return [_row(r) for r in rows]


def active(conn) -> list:
    """The users background duties run for. Local mode: the local user alone, whether or not the row
    is there yet (a fresh install has a scheduler before it has a profile)."""
    if not identity.cloud():
        return [get(conn, identity.LOCAL_USER) or {"id": identity.LOCAL_USER, "role": "admin", "status": "active"}]
    return list_users(conn, status="active")


def create(conn, email: Optional[str], name: str = "", role: str = "rep", team_id: Optional[str] = None,
           status: str = "active", user_id: Optional[str] = None, **profile) -> dict:
    """A new user. Refused on SQLite when any other user exists (one seller per file, in any mode)."""
    if role not in ROLES:
        raise UserError(f"role must be one of {', '.join(ROLES)}")
    if status not in STATUSES:
        raise UserError(f"status must be one of {', '.join(STATUSES)}")
    unknown = set(profile) - set(PROFILE_COLUMNS) - {"google_sub"}
    if unknown:
        raise UserError(f"unknown user fields: {sorted(unknown)}")
    user_id = user_id or new_id()
    if conn.dialect == "sqlite":
        other = conn.execute("SELECT id FROM users WHERE id != ? LIMIT 1", (user_id,)).fetchone()
        if other is not None:
            raise UserError("a SQLite store holds one user; a team needs Postgres (DATABASE_URL)")
    stamp = now()
    conn.execute(
        "INSERT INTO users(id,email,name,role,team_id,status,google_sub,extra_emails,aliases,signature,timezone,"
        "languages,role_title,style,call_context,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, _norm_email(email), (name or "").strip(), role, team_id, status, profile.get("google_sub"),
         _dump(profile.get("extra_emails")), _dump(profile.get("aliases")), profile.get("signature"),
         profile.get("timezone"), _dump(profile.get("languages")), profile.get("role_title"), profile.get("style"),
         profile.get("call_context"), stamp, stamp))
    return get(conn, user_id)


UPDATABLE = ("email", "name", "role", "team_id", "status", "google_sub", *PROFILE_COLUMNS)


def update(conn, user_id: str, **fields) -> dict:
    unknown = set(fields) - set(UPDATABLE)
    if unknown:
        raise UserError(f"unknown user fields: {sorted(unknown)}")
    if "role" in fields and fields["role"] not in ROLES:
        raise UserError(f"role must be one of {', '.join(ROLES)}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise UserError(f"status must be one of {', '.join(STATUSES)}")
    if not fields:
        return get(conn, user_id)
    values = []
    for key, value in fields.items():
        if key in LIST_COLUMNS:
            value = _dump(value)
        elif key == "email":
            value = _norm_email(value)
        elif key == "name":
            value = (value or "").strip()
        values.append(value)
    sets = ", ".join(f"{k}=?" for k in fields)
    cur = conn.execute(f"UPDATE users SET {sets}, updated_at=? WHERE id=?", (*values, now(), user_id))
    if cur.rowcount == 0:
        raise UserError(f"no such user: {user_id}")
    return get(conn, user_id)


def configured(row) -> bool:
    """Enough of a person to coach: a name and one address."""
    p = profile_of(row) if row else {}
    return bool(p.get("name") and p.get("emails"))


# ---- audit ---------------------------------------------------------------------------------------

def audit(conn, kind: str, after: dict, before: Optional[dict] = None, actor_user_id: Optional[str] = None) -> None:
    """An admin or sign-in change, in `events` with actor_user_id set (engine._emit leaves it NULL).
    The row is the acting user's (owner_id from the connection's actor), so bind one first."""
    actor_user_id = actor_user_id or identity.actor_of(conn).user_id
    conn.execute("INSERT INTO events(ts, actor, kind, node_id, before, after, actor_user_id) VALUES (?,?,?,NULL,?,?,?)",
                 (now(), f"user:{actor_user_id}", kind, json.dumps(before) if before is not None else None,
                  json.dumps(after), actor_user_id))


def as_actor(row: dict, mode: str = identity.INTERACTIVE) -> identity.Actor:
    """The Actor a users row acts as."""
    return identity.Actor(row["id"], mode, row["role"], profile_of(row))


# ---- the local user ------------------------------------------------------------------------------

def ensure_local(conn) -> dict:
    """The single user of a local install, created from seller.yaml on first use. Idempotent; never
    called in cloud mode (there is no implicit user there)."""
    row = get(conn, identity.LOCAL_USER)
    if row is not None:
        return row
    from . import seller
    p = seller.user_profile_from_yaml()
    conn.execute(
        "INSERT INTO users(id,email,name,role,status,extra_emails,aliases,signature,timezone,languages,role_title,"
        "style,call_context,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (identity.LOCAL_USER, _norm_email(p["emails"][0]) if p["emails"] else None, p["name"], "admin", "active",
         _dump(p["emails"][1:]), _dump(p["aliases"]), p["signature"] or None, p["timezone"] or None,
         _dump(p["languages"]), p["role_title"] or None, None, p["call_context"] or None, now(), now()))
    conn.commit()
    _adopt_me_labels(conn)
    return get(conn, identity.LOCAL_USER)


def sync_local(conn) -> Optional[dict]:
    """After a profile save on the local install: the users row follows seller.yaml (the row is a shadow;
    seller.profile() still reads the file for the local user)."""
    if get(conn, identity.LOCAL_USER) is None:
        return ensure_local(conn)
    from . import seller
    p = seller.user_profile_from_yaml()
    return update(conn, identity.LOCAL_USER, email=p["emails"][0] if p["emails"] else None, name=p["name"],
                  extra_emails=p["emails"][1:], aliases=p["aliases"], signature=p["signature"] or None,
                  timezone=p["timezone"] or None, languages=p["languages"], role_title=p["role_title"] or None,
                  call_context=p["call_context"] or None)


def _adopt_me_labels(conn) -> None:
    """One-time: the labels sources.yaml remembered for the one seller become the local user's."""
    from . import config
    try:
        data = config.load_user("sources")
    except Exception:
        return
    labels = [str(l) for l in (data.get("me_labels") or []) if l]
    if "me_labels" not in data:
        return
    for label in labels:
        remember_label(conn, identity.LOCAL_USER, label)
    conn.commit()
    data.pop("me_labels", None)
    config.save_user("sources", data)


# ---- teams ----------------------------------------------------------------------------------------

def create_team(conn, name: str, team_id: Optional[str] = None) -> dict:
    team_id = team_id or "t-" + uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO teams(id,name,created_at) VALUES (?,?,?)", (team_id, (name or "").strip(), now()))
    return get_team(conn, team_id)


def get_team(conn, team_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    return dict(row) if row else None


def list_teams(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY name, id").fetchall()]


def set_managers(conn, team_id: str, user_ids) -> list:
    conn.execute("DELETE FROM team_managers WHERE team_id=?", (team_id,))
    for uid in dict.fromkeys(user_ids or ()):
        conn.execute("INSERT INTO team_managers(team_id,user_id) VALUES (?,?)", (team_id, uid))
    return managers_of(conn, team_id)


def managers_of(conn, team_id: str) -> list:
    return [r[0] for r in conn.execute("SELECT user_id FROM team_managers WHERE team_id=? ORDER BY user_id",
                                       (team_id,)).fetchall()]


def teams_managed_by(conn, user_id: str) -> list:
    return [r[0] for r in conn.execute("SELECT team_id FROM team_managers WHERE user_id=? ORDER BY team_id",
                                       (user_id,)).fetchall()]


# ---- speaker labels -------------------------------------------------------------------------------

def _norm_label(label) -> str:
    from .sources.base import norm_label
    return norm_label(label)


def remembered_labels(conn, user_id: Optional[str] = None) -> list:
    user_id = user_id or identity.actor_of(conn).user_id
    return [r[0] for r in conn.execute(
        "SELECT label FROM user_speaker_labels WHERE user_id=? ORDER BY created_at, label_norm", (user_id,)).fetchall()]


def remember_label(conn, user_id: Optional[str], label: str) -> None:
    user_id = user_id or identity.actor_of(conn).user_id
    key = _norm_label(label)
    if not key:
        return
    conn.execute("INSERT INTO user_speaker_labels(user_id,label_norm,label,created_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(user_id,label_norm) DO NOTHING", (user_id, key, label.strip(), now()))


def forget_labels(conn, user_id: Optional[str] = None) -> None:
    user_id = user_id or identity.actor_of(conn).user_id
    conn.execute("DELETE FROM user_speaker_labels WHERE user_id=?", (user_id,))
