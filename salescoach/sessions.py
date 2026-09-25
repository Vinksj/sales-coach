"""Server-side sessions for a cloud install (web/auth.py uses them; hosted.py keeps the
password-mode cookie of a single-seller install, which is unchanged).

The browser holds `<session id>.<hmac>`: a random id signed with SALESCOACH_SESSION_SECRET, and
nothing else. Who the session is, what role they have and whether they are still allowed in is
read from `sessions` and `users` on EVERY request, so disabling a user, revoking one session or
"log out everywhere" takes effect at the next request. Expiry slides: a session used within its
thirty days lives another thirty from that use (written at most once per SLIDE_EVERY_S, so a busy
page is not a write per request).

In cloud mode the secret must be set explicitly: the password-mode fallback (a secret derived from
the password) has nothing to derive from when there is no password.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import hosted, identity
from .store.stores import now

SLIDING_DAYS = 30
SLIDING_S = SLIDING_DAYS * 24 * 3600
SLIDE_EVERY_S = 5 * 60
COOKIE = hosted.COOKIE


class NoSecret(RuntimeError):
    pass


def secret() -> bytes:
    explicit = os.environ.get(hosted.SESSION_SECRET_ENV) or ""
    if explicit:
        return explicit.encode()
    if identity.cloud():
        raise NoSecret(f"{hosted.SESSION_SECRET_ENV} must be set in cloud mode (a long random string)")
    return hosted.session_secret()


def _sign(session_id: str) -> str:
    return hmac.new(secret(), session_id.encode(), hashlib.sha256).hexdigest()


def cookie_value(session_id: str) -> str:
    return f"{session_id}.{_sign(session_id)}"


def session_id_from_cookie(value: Optional[str]) -> Optional[str]:
    """The session id a cookie names, when its signature holds; None for anything else."""
    if not value or not isinstance(value, str) or value.count(".") != 1:
        return None
    session_id, signature = value.split(".")
    if not session_id or not signature:
        return None
    try:
        expected = _sign(session_id)
    except NoSecret:
        return None
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return None
    return session_id


def _ts(moment: Optional[datetime] = None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def create(conn, user_id: str, ip: Optional[str] = None, user_agent: Optional[str] = None,
           at: Optional[datetime] = None) -> tuple[str, str]:
    """A new session for `user_id`. Returns (session id, cookie value). Commits."""
    moment = at or datetime.now(timezone.utc)
    session_id = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(id,user_id,created_at,last_seen_at,expires_at,revoked_at,ip,user_agent) "
                 "VALUES (?,?,?,?,?,NULL,?,?)",
                 (session_id, user_id, _ts(moment), _ts(moment), _ts(moment + timedelta(seconds=SLIDING_S)),
                  (ip or None) and str(ip)[:64], (user_agent or None) and str(user_agent)[:300]))
    conn.commit()
    return session_id, cookie_value(session_id)


def resolve(conn, session_id: Optional[str], at: Optional[datetime] = None) -> Optional[dict]:
    """The live session row for an id: not revoked, not expired, and its expiry slid forward when it
    was last seen more than SLIDE_EVERY_S ago. None otherwise. Commits when it wrote."""
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None or row["revoked_at"]:
        return None
    moment = at or datetime.now(timezone.utc)
    try:
        expired = _parse(row["expires_at"]) <= moment
        seen = _parse(row["last_seen_at"])
    except ValueError:
        return None
    if expired:                                   # closed for good: a later (or earlier) clock cannot revive it
        conn.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (_ts(moment), session_id))
        conn.commit()
        return None
    out = dict(row)
    if (moment - seen).total_seconds() >= SLIDE_EVERY_S:
        out["last_seen_at"], out["expires_at"] = _ts(moment), _ts(moment + timedelta(seconds=SLIDING_S))
        conn.execute("UPDATE sessions SET last_seen_at=?, expires_at=? WHERE id=?",
                     (out["last_seen_at"], out["expires_at"], session_id))
        conn.commit()
    return out


def revoke(conn, session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    cur = conn.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now(), session_id))
    conn.commit()
    return bool(cur.rowcount)


def revoke_all(conn, user_id: str, keep: Optional[str] = None) -> int:
    """Every live session of a user ("log out everywhere", an admin disabling them); `keep` spares
    one (the session pressing the button). Returns how many were revoked."""
    if keep:
        cur = conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND id != ?",
                           (now(), user_id, keep))
    else:
        cur = conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now(), user_id))
    conn.commit()
    return cur.rowcount


def live_for(conn, user_id: str, at: Optional[datetime] = None) -> list[dict]:
    moment = _ts(at)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM sessions WHERE user_id=? AND revoked_at IS NULL AND expires_at > ? ORDER BY last_seen_at DESC",
        (user_id, moment)).fetchall()]


def purge(conn, at: Optional[datetime] = None, keep_days: int = 7) -> int:
    """Housekeeping: drop sessions expired or revoked more than `keep_days` ago."""
    cutoff = _ts((at or datetime.now(timezone.utc)) - timedelta(days=keep_days))
    cur = conn.execute("DELETE FROM sessions WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
                       (cutoff, cutoff))
    conn.commit()
    return cur.rowcount


def set_cookie(response, value: str) -> None:
    response.set_cookie(COOKIE, value, max_age=SLIDING_S, path="/", httponly=True, samesite="lax",
                        secure=hosted.cookie_secure())


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE, path="/")
