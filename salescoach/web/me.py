"""/me/setup: the acting user's own profile (the USER half of seller.py); /me/export: their own data.

The local user's profile lives in seller.yaml and style.md, edited on /setup/you together with the
org's; this page sends the local user there. Any other user (a cloud install) edits their `users`
row here: name, addresses, aliases, signature, timezone, languages, role title, call context and
their own style guide. The org's fields are an admin's, on /setup.

The FirstRunGate sends a user here when the org is set up but they are not (web/app.py).
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import budget, config, googleauth, identity, repo, seller, sessions, users
from ..execution import tokens
from ..setupui import forms

router = APIRouter()
PATH = "/me/setup"
DISCONNECT = "/me/connections/google/disconnect"


def _connections(conn, actor) -> dict:
    """The Google card's facts for a cloud user: org-level readiness and this user's grant."""
    if not identity.cloud():
        return {}
    status = tokens.status_of(conn, actor.user_id)
    return {"google_ready": googleauth.configured() and tokens.keys_configured(),
            "google_problems": googleauth.problems() + ([f"{tokens.KEYS_ENV} is not set"] if not tokens.keys_configured() else []),
            "grant": status, "features": [(key, googleauth.FEATURE_LABELS[key], key in status["features"])
                                          for key in googleauth.FEATURE_SCOPES],
            "sessions_live": len(sessions.live_for(conn, actor.user_id))}


def _web():
    from . import app as webapp
    return webapp


def _recorders(conn) -> dict:
    """The "Your call recorder" cards (Phase 4, sources/connections.py): cloud only; public fields only."""
    if not identity.cloud():
        return {}
    from ..sources import connections
    return connections.card_context(conn)


def _page(request: Request, values: dict, style: str, errors=None, status: int = 200, **extra):
    webapp = _web()
    with webapp._db(request) as conn:
        response = webapp.render(request, conn, "me_setup.html", p=values, style=style, errors=errors or {},
                                 timezones=forms.timezones(), default_timezone=seller.DEFAULT_TIMEZONE,
                                 configured=seller.user_configured(), usage=budget.usage_today(conn, mine_only=True),
                                 **_connections(conn, identity.current_actor()), **_recorders(conn), **extra)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(PATH, response_class=HTMLResponse)
def me_setup(request: Request):
    actor = identity.current_actor()
    if actor.is_local:
        return RedirectResponse("/setup/you", status_code=303)
    return _page(request, seller.profile(), seller.style_guide())


@router.post(PATH)
def me_save(request: Request, name: str = Form(""), emails: str = Form(""), role: str = Form(""),
            aliases: str = Form(""), languages: str = Form(""), timezone: str = Form(""),
            signature: str = Form(""), call_context: str = Form(""), style: str = Form("")):
    actor = identity.current_actor()
    if actor.is_local:
        return RedirectResponse("/setup/you", status_code=303)
    org = seller.org_profile()                       # the parser wants the whole profile; the org half is not ours to edit
    data, errors = forms.parse_profile(dict(
        name=name, emails=emails, role=role, aliases=aliases, languages=languages, timezone=timezone,
        signature=signature, call_context=call_context, company=org["company"], website=org["website"],
        offering=org["offering"], icp=org["icp"], buyer_titles=org["buyer_titles"], vocabulary=org["vocabulary"],
        own_domains=",".join(org["own_domains"])))
    style_text, style_error = forms.parse_style(style)
    if style_error:
        errors["style"] = style_error
    if errors:
        return _page(request, {**seller.profile(), **{k: data[k] for k in seller.USER_FIELDS}}, style, errors, status=400)
    webapp = _web()
    with webapp._db(request) as conn:
        shipped = config.text("style.md")
        # The sign-in address (users.email) is an admin's to change: sign-in resolves users by it, so a user who
        # rewrote their own could squat a colleague's before the colleague is invited. The form edits the OTHER
        # addresses; the sign-in address stays first whatever was submitted (trg_users_guard refuses it too).
        primary = (users.get(conn, actor.user_id) or {}).get("email")
        extras = [e for e in data["emails"] if e != primary]
        users.update(conn, actor.user_id, name=data["name"], extra_emails=extras,
                     aliases=data["aliases"], languages=data["languages"], timezone=data["timezone"] or None,
                     signature=data["signature"] or None, role_title=data["role"] or None,
                     call_context=data["call_context"] or None,
                     style=None if forms.same_text(style_text, shipped) or not style_text.strip() else style_text)
        identity.refresh(conn)
        repo.sync_me(conn)
        conn.commit()
    note = "" if not primary or primary in data["emails"] else f" Your sign-in address stays {primary}; an admin changes it."
    return webapp._redirect(PATH, msg="Profile saved." + note)


@router.post(DISCONNECT)
def google_disconnect(request: Request):
    """Revoke the grant at Google (best effort) and forget it here. The row goes whatever Google said."""
    actor = identity.current_actor()
    if actor.is_local or not identity.cloud():
        return RedirectResponse("/setup/connections", status_code=303)
    webapp = _web()
    with webapp._db(request) as conn:
        outcome = tokens.disconnect(conn, actor.user_id)
        if outcome["had"]:
            users.audit(conn, "google.disconnect", {"revoked_at_google": outcome["revoked"]})
        conn.commit()
    if not outcome["had"]:
        return webapp._redirect(PATH, msg="Google was not connected.")
    note = "" if outcome["revoked"] else " Google did not confirm the revocation; remove the coach under your Google account's third-party access too."
    return webapp._redirect(PATH, msg="Google disconnected." + note)


@router.get("/me/export")
def me_export(request: Request):
    """The acting user's own rows as a zip of JSON files, streamed (lifecycle/export.py). Every query names the
    user, so a manager downloads their own work, not their team's."""
    from fastapi.responses import StreamingResponse
    from ..lifecycle import export
    actor = identity.current_actor()
    return StreamingResponse(export.stream_as(actor), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{export.filename(actor.user_id)}"',
                                      "Cache-Control": "no-store"})
