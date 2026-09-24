"""/me/setup: the acting user's own profile (the USER half of seller.py).

The local user's profile lives in seller.yaml and style.md, edited on /setup/you together with the
org's; this page sends the local user there. Any other user (a cloud install) edits their `users`
row here: name, addresses, aliases, signature, timezone, languages, role title, call context and
their own style guide. The org's fields are an admin's, on /setup.

The FirstRunGate sends a user here when the org is set up but they are not (web/app.py).
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, identity, repo, seller, users
from ..setupui import forms

router = APIRouter()
PATH = "/me/setup"


def _web():
    from . import app as webapp
    return webapp


def _page(request: Request, values: dict, style: str, errors=None, status: int = 200):
    webapp = _web()
    with webapp._db(request) as conn:
        response = webapp.render(request, conn, "me_setup.html", p=values, style=style, errors=errors or {},
                                 timezones=forms.timezones(), default_timezone=seller.DEFAULT_TIMEZONE,
                                 configured=seller.user_configured())
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
        users.update(conn, actor.user_id, name=data["name"], email=data["emails"][0], extra_emails=data["emails"][1:],
                     aliases=data["aliases"], languages=data["languages"], timezone=data["timezone"] or None,
                     signature=data["signature"] or None, role_title=data["role"] or None,
                     call_context=data["call_context"] or None,
                     style=None if forms.same_text(style_text, shipped) or not style_text.strip() else style_text)
        identity.refresh(conn)
        repo.sync_me(conn)
        conn.commit()
    return webapp._redirect(PATH, msg="Profile saved.")
