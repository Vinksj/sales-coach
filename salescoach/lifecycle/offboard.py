"""A rep leaves (Admin: Offboard). Two ways, both final:

  reassign  every OWNED row of the leaving user moves to another ACTIVE REP in one transaction (owner_id
            rewritten), except what describes the person rather than the work, which is deleted: their learned
            patterns and proposals, pattern observations, seller patterns and observations, coach reports, live
            coach nudges and state, calendar cache and meetings, recorder connections (tenancy.PERSONAL), the
            coaching notes about them, their user_state and speaker labels. Comments on the moved work move with
            it and keep their author. The receiver's managers now read the moved work; the leaving rep's
            managers who do not manage the receiver no longer do (the read policy follows owner_id).
  purge     every OWNED row of the leaving user is deleted, with their user_state, speaker labels and the
            access log of their objects. Deals, accounts and people in the shared directory stay.
Both also disable the user, revoke every session and every Google grant, and write audit events
(admin.user.offboard with the per-table counts, then admin.user.disable).

An admin reads no rep's content and row-level security lets nobody move a row to another owner, so the data
change is one SECURITY DEFINER function, app_offboard (store/rls.py), which refuses anyone but an active admin
acting interactively and is generated from tenancy.py (a new OWNED table is covered without anyone editing it).
A single-user install has nobody to hand work to: offboarding is for the cloud install (Postgres).
"""
import json

from .. import sessions, users
from ..execution import tokens
from ..store import db

MODES = ("reassign", "purge")


class OffboardError(ValueError):
    """Refused; the message is for the admin. Nothing was changed."""


def check(conn, user_id: str, mode: str, to_user_id=None) -> tuple:
    """(leaving user row, receiving user row or None), or OffboardError."""
    from .. import identity
    if conn.dialect != "postgres":
        raise OffboardError("offboarding is for the cloud install: a single-user install has nobody to hand work to")
    if mode not in MODES:
        raise OffboardError("choose reassign or purge")
    me = identity.actor_of(conn).user_id
    leaving = users.get(conn, user_id)
    if leaving is None:
        raise OffboardError("no such user")
    if user_id == me:
        raise OffboardError("you cannot offboard yourself; ask another admin")
    receiver = None
    if mode == "reassign":
        receiver = users.get(conn, to_user_id) if to_user_id else None
        if receiver is None or receiver["id"] == user_id:
            raise OffboardError("choose who receives the work")
        if receiver["status"] != "active" or receiver["role"] != "rep":
            raise OffboardError(f"{receiver['email'] or receiver['id']} is not an active rep: the work can only go to one")
    return leaving, receiver


def offboard(conn, user_id: str, mode: str, to_user_id=None) -> dict:
    """Do it. The data change, the disabling and the audit commit together; sessions and grants are then revoked
    (each of those commits on its own). Returns {"counts": {...}, "sessions_revoked": n, "grants_revoked": n}."""
    leaving, receiver = check(conn, user_id, mode, to_user_id)
    try:
        raw = conn.execute("SELECT app_offboard(?, ?)", (user_id, receiver["id"] if receiver else None)).fetchone()[0]
    except db.Error as exc:
        conn.rollback()
        message = getattr(getattr(exc, "diag", None), "message_primary", None) or type(exc).__name__
        raise OffboardError(f"offboarding was refused: {message}") from None
    counts = {k: v for k, v in json.loads(raw or "{}").items() if v}
    if leaving["status"] != "disabled":
        users.update(conn, user_id, status="disabled")
    users.audit(conn, "admin.user.offboard",
                {"user_id": user_id, "mode": mode, "to_user_id": receiver["id"] if receiver else None,
                 "counts": counts}, before={"status": leaving["status"]})
    conn.commit()
    ended = sessions.revoke_all(conn, user_id)
    revoked = tokens.revoke_all_for_user(conn, user_id)
    users.audit(conn, "admin.user.disable", {"user_id": user_id, "sessions_revoked": ended, "grants_revoked": revoked,
                                             "by": "offboard"}, before={"status": leaving["status"]})
    conn.commit()
    return {"counts": counts, "sessions_revoked": ended, "grants_revoked": revoked}
