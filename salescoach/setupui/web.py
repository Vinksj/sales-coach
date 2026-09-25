"""Routes of the setup wizard / Settings.

  GET  /setup                                  the profile step until it is filled, then the review
  GET  /setup/you            POST /setup/you
  GET  /setup/method         POST /setup/method                       choose (methodology.switch)
  GET  /setup/method/custom[/{key}]   POST /setup/method/custom[/{key}]   the builder (save_custom)
                             POST /setup/method/custom/{key}/delete
  GET  /setup/model          POST /setup/model/models | /test | /use  JSON for setup.js, a page without it
  GET  /setup/sources        POST /setup/sources/{kind}               one source's choice
                             POST /setup/sources/webhook/secret       create or replace; shown ONCE
  GET  /setup/connections                                             read-only
  GET  /setup/review         POST /setup/finish
                             POST /setup/dismiss-card                 the Today card

Every POST passes the app's same-origin guard like any other non-GET route.

Cloud mode: these pages are the ORG's settings (the company, the models, the sources, the method,
the budgets), which live in org_settings, and only an active admin may write that table (store/rls.py).
So every route here answers 403 to anyone else, except the per-user Today-card dismissal; a user's own
half is /me/setup.

Secrets. An API key arrives in a POST body and goes straight to config.set_secret; an empty field
leaves the stored key alone. No handler puts a key in a template, a redirect, a flash message, a
log line or the state table, and provider error text is scrubbed of the key before it is shown.
The one exception, by design, is the webhook secret: the response to the POST that creates it
shows it once (no-store), and nothing can show it again.
"""
import json
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import config, identity, providers, repo, seller, sources, users
from ..intel import methodology
from ..providers.base import ProviderError
from ..store.stores import now, set_state, set_user_state
from . import forms, state, words

PER_USER_PATHS = ("/setup/dismiss-card",)
NOT_AN_ADMIN = ("Settings are managed by an admin on this install. If the coach is not set up yet, ask your admin "
                "to finish Settings; your own profile is under You.")


def _admins_only_in_cloud(request: Request) -> None:
    if identity.cloud() and request.url.path not in PER_USER_PATHS:
        actor = identity.current_actor(required=False)
        if actor is None or actor.role != "admin":
            raise HTTPException(status_code=403, detail=NOT_AN_ADMIN)


router = APIRouter(dependencies=[Depends(_admins_only_in_cloud)])

PROVIDER_COPY = words.PROVIDER_COPY
TIER_HELP = "Heavy does the analysis and the drafting; light does the quick checks and the live coach."
SAMPLE_PAYLOAD = ('{"id": "meeting-7781", "source": "my-recorder", "title": "Acme discovery", '
                  '"participants": [{"name": "Asha Rao", "email": "asha.rao@acme.example"}], '
                  '"turns": [{"speaker": "Asha Rao", "text": "We lose two days on every dispute."}, '
                  '{"speaker": "Me", "text": "I will send a one-page plan by Friday."}]}')


def _web():
    from ..web import app as webapp
    return webapp


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _page(request: Request, template: str, step: str, status: int = 200, build=None, **ctx):
    """Render one step inside the wizard frame. `build(conn, live, rows)` adds what needs the database;
    `rows` are the connection checks, made once per page (the rail needs them on every step)."""
    webapp = _web()
    with webapp._db(request) as conn:
        live = webapp._live_status(request.app)
        rows = state.connections(conn, live)
        if build:
            ctx.update(build(conn, live, rows))
        back, forward = state.neighbours(step)
        response = webapp.render(request, conn, template, step=step, rail=state.steps(conn, live, rows),
                                 step_title=state.TITLES[step], step_number=[k for k, _ in state.STEPS].index(step) + 1,
                                 step_count=len(state.STEPS), back_href=state.href(back) if back else None,
                                 next_href=state.href(forward) if forward else None,
                                 configured=seller.is_configured(), **ctx)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _go(step: str, go: str, msg=None, err=None, anchor=None, stay_url=None):
    """After a save: on to the next step ("Save and continue") or back to this one ("Save")."""
    _, forward = state.neighbours(step)
    url = state.href(forward) if (go == "next" and forward and not err) else (stay_url or state.href(step))
    return _web()._redirect(url, msg=msg, err=err, anchor=None if (go == "next" and not err) else anchor)


def _scrub(text, *values) -> str:
    """Provider error text with anything that is, or was just typed as, a key removed."""
    out = str(text or "")
    for value in values:
        if value and len(value) >= 4:
            out = out.replace(value, "[key]")
    return out[:500]


# ------------------------------------------------------------------------------ entry + step 1

@router.get("/setup", response_class=HTMLResponse)
def setup_home(request: Request):
    return review_page(request) if seller.is_configured() else you_page(request)


def _you(request, values, style, errors=None, status=200):
    return _page(request, "setup_you.html", "you", status=status, p=values, style=style, errors=errors or {},
                 timezones=forms.timezones(), default_timezone=seller.DEFAULT_TIMEZONE,
                 style_is_own=(config.user_dir() / "style.md").exists())


@router.get("/setup/you", response_class=HTMLResponse)
def you_page(request: Request):
    return _you(request, seller.profile(), config.text("style.md"))


@router.post("/setup/you")
def you_save(request: Request, name: str = Form(""), emails: str = Form(""), company: str = Form(""),
             role: str = Form(""), website: str = Form(""), offering: str = Form(""), icp: str = Form(""),
             buyer_titles: str = Form(""), vocabulary: str = Form(""), own_domains: str = Form(""),
             aliases: str = Form(""), languages: str = Form(""), timezone: str = Form(""),
             signature: str = Form(""), call_context: str = Form(""), style: str = Form(""),
             go: str = Form("next")):
    data, errors = forms.parse_profile(dict(
        name=name, emails=emails, company=company, role=role, website=website, offering=offering, icp=icp,
        buyer_titles=buyer_titles, vocabulary=vocabulary, own_domains=own_domains, aliases=aliases,
        languages=languages, timezone=timezone, signature=signature, call_context=call_context))
    style_text, style_error = forms.parse_style(style)
    if style_error:
        errors["style"] = style_error
    if errors:
        return _you(request, {**seller.profile(), **data}, style, errors, status=400)

    # Keep whatever else the user's seller.yaml holds; drop empty fields so tracked defaults still apply.
    saved = {**config.load_user("seller"), **data}
    config.save_user("seller", {k: v for k, v in saved.items() if v not in ("", [], None)})
    if not forms.same_text(style_text, config.text("style.md")):
        if style_text.strip():
            config.save_user_text("style.md", style_text)
        else:                                       # cleared: back to the guide the coach ships with
            (config.user_dir() / "style.md").unlink(missing_ok=True)
    webapp = _web()
    with webapp._db(request) as conn:
        if not identity.cloud():                    # the local user's row shadows seller.yaml; cloud users have rows of their own
            users.sync_local(conn)
        repo.sync_me(conn)
        conn.commit()
    return _go("you", go, msg="Profile saved.")


# ---------------------------------------------------------------------------- step 2: methodology

def _custom_definition(key: str) -> dict | None:
    custom = config.load_user("methodology").get("custom")
    found = custom.get(key) if isinstance(custom, dict) else None
    return found if isinstance(found, dict) else None


@router.get("/setup/method", response_class=HTMLResponse)
def method_page(request: Request):
    return _page(request, "setup_method.html", "method", methods=methodology.available(),
                 active_key=methodology.active_key(),
                 chosen=bool(config.load_user("methodology").get("active")))


def _deals_phrase(n: int) -> str:
    if not n:
        return "No open deal needs re-reading."
    return f"{n} open deal{'' if n == 1 else 's'} will be re-read against it; that takes a few minutes."


@router.post("/setup/method")
def method_choose(request: Request, key: str = Form(""), go: str = Form("next")):
    key = key.strip()
    found = methodology.get(key)
    if found is None:
        return _go("method", "stay", err="Choose one of the methodologies on this page.")
    if key == methodology.active_key():
        if not config.load_user("methodology").get("active"):
            methodology.set_active(key)               # records the choice; nothing changes for any deal
        return _go("method", go, msg=f"Selling with {found.name}.")
    webapp = _web()
    with webapp._db(request) as conn:
        queued = methodology.switch(conn, key)
    return _go("method", go, msg=f"Now selling with {found.name}. {_deals_phrase(queued)}")


def _builder(request, definition, rows, editing, field_errors=None, general=None, status=200, focus_row=None):
    return _page(request, "setup_method_custom.html", "method", status=status, d=definition, rows=rows,
                 editing=editing, errors=field_errors or {}, general=general or [], kinds=methodology.KINDS,
                 max_rows=forms.MAX_ROWS, focus_row=focus_row,
                 is_active=bool(editing) and methodology.active_key() == editing)


@router.get("/setup/method/custom", response_class=HTMLResponse)
def custom_new(request: Request):
    return _builder(request, {"name": "", "key": "", "kind": "qualification", "description": ""},
                    forms.rows_from_definition(None), editing=None)


@router.get("/setup/method/custom/{key}", response_class=HTMLResponse)
def custom_edit(request: Request, key: str):
    stored = _custom_definition(key)
    if stored is None:
        return _web()._redirect("/setup/method", err="That is not one of your own methodologies.")
    return _builder(request, {**stored, "key": key}, forms.rows_from_definition(stored), editing=key)


def _custom_save(request, editing, form, action, activate, go):
    existing = _custom_definition(editing) if editing else None
    if editing and existing is None:
        return _web()._redirect("/setup/method", err="That is not one of your own methodologies.")
    definition, rows, row_map = forms.parse_methodology(form, existing, fixed_key=editing)
    if action == "add":                             # one more empty row; nothing is checked or saved
        rows = forms.pad_rows(rows, extra=1)
        return _builder(request, definition, rows, editing, focus_row=len(rows) - 1)

    errors = methodology.validate_definition(definition)
    if not editing and not errors and methodology.get(definition["key"]) is not None:
        errors.append(f"methodology: key '{definition['key']}' is already taken; change the name or the key")
    if not errors:
        try:
            methodology.save_custom(definition)
        except methodology.InvalidMethodology as exc:
            errors = exc.errors
    if errors:
        if not definition["name"]:                  # the id is made from the name: one message, not two
            errors = [e for e in errors if e != "methodology: key is required"]
        field_errors, general = forms.map_methodology_errors(errors, row_map)
        return _builder(request, definition, rows, editing, field_errors, general, status=400)

    key, name = definition["key"], definition["name"]
    if activate or methodology.active_key() == key:
        webapp = _web()
        with webapp._db(request) as conn:
            queued = methodology.switch(conn, key)
        return _go("method", go, msg=f"{name} is saved and in use. {_deals_phrase(queued)}")
    return _go("method", "stay", msg=f"{name} is saved. Choose it below to start selling with it.",
               anchor=f"m-{key}")


def _builder_form(name, key, kind, description, el_key, el_label, el_known_when, el_partial_when, el_questions,
                  el_cap, el_critical):
    return dict(name=name, key=key, kind=kind, description=description, el_key=el_key, el_label=el_label,
                el_known_when=el_known_when, el_partial_when=el_partial_when, el_questions=el_questions,
                el_cap=el_cap, el_critical=el_critical)


@router.post("/setup/method/custom")
def custom_create(request: Request, name: str = Form(""), key: str = Form(""), kind: str = Form("qualification"),
                  description: str = Form(""), el_key: list[str] = Form(default=[]),
                  el_label: list[str] = Form(default=[]), el_known_when: list[str] = Form(default=[]),
                  el_partial_when: list[str] = Form(default=[]), el_questions: list[str] = Form(default=[]),
                  el_cap: list[str] = Form(default=[]), el_critical: list[str] = Form(default=[]),
                  action: str = Form("save"), activate: str = Form(""), go: str = Form("stay")):
    form = _builder_form(name, key, kind, description, el_key, el_label, el_known_when, el_partial_when,
                         el_questions, el_cap, el_critical)
    return _custom_save(request, None, form, action, bool(activate), go)


@router.post("/setup/method/custom/{editing}")
def custom_update(request: Request, editing: str, name: str = Form(""), kind: str = Form("qualification"),
                  description: str = Form(""), el_key: list[str] = Form(default=[]),
                  el_label: list[str] = Form(default=[]), el_known_when: list[str] = Form(default=[]),
                  el_partial_when: list[str] = Form(default=[]), el_questions: list[str] = Form(default=[]),
                  el_cap: list[str] = Form(default=[]), el_critical: list[str] = Form(default=[]),
                  action: str = Form("save"), activate: str = Form(""), go: str = Form("stay")):
    form = _builder_form(name, editing, kind, description, el_key, el_label, el_known_when, el_partial_when,
                         el_questions, el_cap, el_critical)
    return _custom_save(request, editing, form, action, bool(activate), go)


@router.post("/setup/method/custom/{key}/delete")
def custom_delete(request: Request, key: str):
    stored = _custom_definition(key)
    try:
        methodology.delete_custom(key)
    except KeyError:
        return _web()._redirect("/setup/method", err="That is not one of your own methodologies.")
    except ValueError:
        name = (stored or {}).get("name") or key
        return _web()._redirect("/setup/method", anchor=f"m-{key}",
                                err=f"{name} is the methodology in use. Choose another one first, then delete it.")
    return _web()._redirect("/setup/method", msg=f"{(stored or {}).get('name') or key} was deleted.")


# ---------------------------------------------------------------------------------- step 3: model

def _descriptor(key: str) -> dict | None:
    return next((d for d in providers.catalog() if d["key"] == key), None)


def _model_context(conn, selected_key, models=None, models_error=None, test=None, key_error=None, form=None):
    catalog = providers.catalog()
    selected = next((d for d in catalog if d["key"] == selected_key), None) or \
        next((d for d in catalog if d["active"]), catalog[0])
    if models is None and selected["key"] == "claude_code":
        try:
            models = providers.list_models("claude_code")         # a fixed list: no request is made
        except ProviderError:
            models = None
    form = form or {}
    tiers = {t: form.get(t) or selected["tiers"].get(t) or "" for t in providers.TIERS}
    options = list(dict.fromkeys([*(models or []), *[m for m in tiers.values() if m]]))
    shown_test = test
    if shown_test is None:
        last = state.last_test(conn)
        shown_test = last if last and last.get("provider") == selected["key"] else None
    from .. import budget
    return {"catalog": catalog, "selected": selected, "copy": PROVIDER_COPY, "tier_help": TIER_HELP,
            "usage": budget.usage_today(conn),
            "models": options, "models_loaded": models is not None, "models_error": models_error,
            "tiers": tiers, "base_url": form.get("base_url") if form.get("base_url") is not None else selected["base_url"],
            "test": shown_test, "key_error": key_error, "provider_state": state.provider_state(conn)}


def _model_page(request, selected_key, status=200, **kw):
    return _page(request, "setup_model.html", "model", status=status,
                 build=lambda conn, live, rows: _model_context(conn, selected_key, **kw))


@router.get("/setup/model", response_class=HTMLResponse)
def model_page(request: Request, provider: str = ""):
    return _model_page(request, provider)


def _store_key(descriptor: dict, api_key: str) -> str | None:
    """Store a typed key. Empty = keep what is stored. Returns a message when the key was refused."""
    value = (api_key or "").strip()
    if not value or not descriptor.get("api_key_env"):
        return None
    try:
        config.set_secret(descriptor["api_key_env"], value)
    except ValueError:
        return "That does not look like an API key: it cannot contain quotes, spaces or line breaks. Nothing was stored."
    return None


MODEL_NOT_ID = ("That is not a model id: it looks like an API key, or is too long. Paste the key into the API key "
                "field and pick a model from the list. Nothing was sent or stored.")
HOST_CHANGED = ("The endpoint's address changed ({old} to {new}) and a key is stored for {old}. Enter the API key "
                "again to confirm it should be sent to {new}; nothing was sent.")
KEY_HOST_KEY = "setup:key_host:{provider}"


def _model_cfg(descriptor, base_url, heavy, light, heavy_custom, light_custom) -> tuple[dict, dict, str | None]:
    """(cfg for the provider functions, the tier values as typed, problem). A tier value that looks like an
    API key is dropped here, before it can be sent, stored in the state table or shown back on the page."""
    from ..providers.setup import check_model_id
    tiers, problem = {}, None
    for tier, value in (("heavy", heavy_custom or heavy), ("light", light_custom or light)):
        try:
            tiers[tier] = check_model_id(value)
        except ProviderError:
            tiers[tier], problem = "", MODEL_NOT_ID
    cfg = {"tiers": {t: m for t, m in tiers.items() if m}}
    if descriptor["base_url_editable"]:
        cfg["base_url"] = (base_url or "").strip()
    return cfg, tiers, problem


def _host(url) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def _host_change(request, descriptor, cfg, api_key) -> str | None:
    """A stored key goes wherever base_url points. When the HOST of an editable base_url differs from
    the host the key was entered for (recorded when it was typed, else the saved base_url's), and no key
    was typed this time, nothing is sent until the key is re-entered. Typing the key records the host."""
    if not descriptor["base_url_editable"] or not descriptor.get("api_key_env"):
        return None
    new = _host(cfg.get("base_url"))
    if not new:
        return None
    webapp = _web()
    key = KEY_HOST_KEY.format(provider=descriptor["key"])
    with webapp._db(request) as conn:
        known = state.get_state(conn, key) or _host(descriptor.get("base_url"))
        if (api_key or "").strip() or not descriptor["key_set"] or not known:
            if descriptor["key_set"] or (api_key or "").strip():
                set_state(conn, key, new)             # the key was (just) entered for THIS host
                conn.commit()
            return None
        if known == new:
            return None
    return HOST_CHANGED.format(old=known, new=new)


def _known_secrets(descriptor, api_key) -> tuple:
    stored = config.secret(descriptor["api_key_env"]) if descriptor.get("api_key_env") else None
    return ((api_key or "").strip(), stored)


@router.post("/setup/model/models")
def model_list(request: Request, provider: str = Form(""), api_key: str = Form(""), base_url: str = Form(""),
               heavy: str = Form(""), light: str = Form(""), heavy_custom: str = Form(""),
               light_custom: str = Form("")):
    d = _descriptor(provider)
    if d is None:
        return JSONResponse({"ok": False, "error": "Unknown provider."}, status_code=400) if _wants_json(request) \
            else _web()._redirect("/setup/model", err="Choose one of the providers on this page.")
    cfg, tiers, problem = _model_cfg(d, base_url, heavy, light, heavy_custom, light_custom)
    key_error = problem or _store_key(d, api_key)
    if not key_error:
        d = _descriptor(provider)
        key_error = _host_change(request, d, cfg, api_key)
    models, error = None, key_error
    if not key_error:
        try:
            models = providers.list_models(provider, cfg)
        except ProviderError as exc:
            error = _scrub(exc, *_known_secrets(d, api_key))
    d = _descriptor(provider)
    if _wants_json(request):
        return JSONResponse({"ok": models is not None, "models": models or [], "error": error,
                             "key_set": d["key_set"]}, headers={"Cache-Control": "no-store"})
    return _model_page(request, provider, models=models, models_error=None if key_error else error,
                       key_error=key_error, form={**tiers, "base_url": cfg.get("base_url")})


@router.post("/setup/model/test")
def model_test(request: Request, provider: str = Form(""), api_key: str = Form(""), base_url: str = Form(""),
               heavy: str = Form(""), light: str = Form(""), heavy_custom: str = Form(""),
               light_custom: str = Form("")):
    d = _descriptor(provider)
    if d is None:
        return JSONResponse({"ok": False, "error": "Unknown provider."}, status_code=400) if _wants_json(request) \
            else _web()._redirect("/setup/model", err="Choose one of the providers on this page.")
    cfg, tiers, problem = _model_cfg(d, base_url, heavy, light, heavy_custom, light_custom)
    key_error = problem or _store_key(d, api_key)
    if not key_error:
        d = _descriptor(provider)
        key_error = _host_change(request, d, cfg, api_key)
    if key_error:
        result = {"ok": False, "model": None, "latency_ms": None, "error": key_error}
    else:
        result = dict(providers.test_connection(provider, cfg))
        result["error"] = _scrub(result.get("error"), *_known_secrets(d, api_key)) or None
        record = {"provider": provider, "ok": bool(result.get("ok")), "model": result.get("model"),
                  "latency_ms": result.get("latency_ms"), "error": result["error"], "at": now()}
        webapp = _web()
        with webapp._db(request) as conn:
            set_state(conn, state.TEST_KEY, json.dumps(record))
            conn.commit()
        result = record
    d = _descriptor(provider)
    if _wants_json(request):
        return JSONResponse({**result, "key_set": d["key_set"]}, headers={"Cache-Control": "no-store"})
    return _model_page(request, provider, test={**result, "provider": provider},
                       key_error=key_error, form={**tiers, "base_url": cfg.get("base_url")})


@router.post("/setup/model/use")
def model_use(request: Request, provider: str = Form(""), api_key: str = Form(""), base_url: str = Form(""),
              heavy: str = Form(""), light: str = Form(""), heavy_custom: str = Form(""),
              light_custom: str = Form(""), go: str = Form("stay")):
    d = _descriptor(provider)
    if d is None:
        return _web()._redirect("/setup/model", err="Choose one of the providers on this page.")
    here = f"/setup/model?provider={provider}"
    cfg, tiers, problem = _model_cfg(d, base_url, heavy, light, heavy_custom, light_custom)
    problem = problem or _store_key(d, api_key)
    d = _descriptor(provider)
    if not problem:
        problem = _host_change(request, d, cfg, api_key)
    if not problem:
        if provider == "claude_code" and not d["available"]:
            problem = "The Claude CLI is not installed on this machine, so this option cannot be used yet."
        elif d["needs_key"] and not d["key_set"]:
            problem = "Add an API key first."
        elif d["base_url_editable"] and not cfg.get("base_url"):
            problem = "Fill in the address of your endpoint first."
        elif not all(tiers.values()):
            problem = "Choose a model for both tiers first. Press Load models to see the list."
    if not problem:
        try:
            providers.save_choice(provider, {k: v for k, v in cfg.items() if k != "tiers"}, tiers)
        except ValueError as exc:
            problem = _scrub(exc, *_known_secrets(d, api_key))
    if problem:
        return _go("model", "stay", err=problem, stay_url=here)
    label = PROVIDER_COPY.get(provider, {}).get("name") or d["label"]
    return _go("model", go, msg=f"The coach now uses {label}.", stay_url=here)


# -------------------------------------------------------------------------------- step 4: sources

def _webhook_url(request: Request) -> str:
    from .. import hosted
    from ..sources.adapters import webhook
    base = hosted.public_origin() or str(request.base_url).rstrip("/")     # hosted: the address the world uses
    return base + webhook.PATH


def _sources_context(request, conn, live, new_secret=None):
    from ..sources.adapters import webhook
    catalog = sources.catalog(conn)
    url = _webhook_url(request)
    return {"push": [d for d in catalog if d["mode"] == "push"], "poll": [d for d in catalog if d["mode"] == "poll"],
            "export": [d for d in catalog if d["mode"] == "export"], "live": live,
            "capture_built": state.capture_binary().is_file(), "drop_dir": str(state.drop_dir()),
            "webhook_url": url, "webhook_header": "X-Salescoach-Secret", "new_secret": new_secret,
            "sample_curl": (f"curl -X POST {url} \\\n  -H 'Content-Type: application/json' \\\n"
                            f"  -H 'X-Salescoach-Secret: YOUR-SECRET' \\\n  -d '{SAMPLE_PAYLOAD}'"),
            "cli": providers.claude_cli_available(), "webhook_secret_name": webhook.SECRET_NAME,
            "min_poll": sources.MIN_POLL_MINUTES, "max_poll": sources.MAX_POLL_MINUTES}


@router.get("/setup/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    return _page(request, "setup_sources.html", "sources",
                 build=lambda conn, live, rows: _sources_context(request, conn, live))


@router.post("/setup/sources/webhook/secret", response_class=HTMLResponse)
def webhook_secret(request: Request):
    secret = sources.new_webhook_secret()
    # Rendered straight into this one response, never redirected to: a redirect would need the secret in
    # a URL or in storage. Reloading the page cannot show it again.
    return _page(request, "setup_sources.html", "sources",
                 build=lambda conn, live, rows: _sources_context(request, conn, live, new_secret=secret))


def _folder_path(raw: str) -> tuple[str, str | None]:
    text = (raw or "").strip()
    if not text:
        return "", None
    if len(text) > 400 or re.search(r"[\x00-\x1f\x7f]", text):
        return text, "That folder path cannot be used."
    from ..sources.adapters import folder
    return text, folder.refusal(text)               # home, the disk, Desktop/Documents/Downloads, the coach's own folders


@router.post("/setup/sources/{kind}")
def source_save(request: Request, kind: str, enabled: str = Form(""), poll_minutes: str = Form(""),
                api_key: str = Form(""), only_deals: str = Form(""), path: str = Form(""),
                allow_remote: str = Form(""), remote_form: str = Form("")):
    anchor = f"src-{kind}" if re.fullmatch(r"[a-z_]+", kind) else None
    d = next((x for x in sources.catalog() if x["kind"] == kind and x["mode"] != "export"), None)
    if d is None:
        return _go("sources", "stay", err="That is not a source the coach can switch on.")
    typed = api_key.strip()
    if typed and d["needs_key"] and kind != "webhook":           # the webhook's secret is generated, never typed
        try:
            config.set_secret(d["api_key_env"], typed)
        except ValueError:
            return _go("sources", "stay", anchor=anchor,
                       err="That does not look like an API key: it cannot contain quotes, spaces or line breaks.")
    want = bool(enabled)
    if want and d["needs_key"] and kind != "webhook" and not config.has_secret(d["api_key_env"]):
        return _go("sources", "stay", anchor=anchor, err=f"Add your {d['label']} API key before switching it on.")
    options = dict(d["options"])
    if d["mode"] == "poll" and kind != "folder":
        options["only_deals"] = bool(only_deals)
    if kind == "webhook" and remote_form:
        # Only the "through a tunnel" form changes this (the on/off button must not reset it). Off unless
        # its box is ticked: the webhook then answers this machine only (webhook.reachable).
        options["allow_remote"] = bool(allow_remote)
        want = d["enabled"]
    if kind == "folder":
        folder, problem = _folder_path(path)
        if problem:
            return _go("sources", "stay", anchor=anchor, err=problem)
        options.pop("path", None)
        if folder:
            options["path"] = folder
    try:
        sources.save(kind, want, poll_minutes.strip() or None, options)
    except ValueError as exc:
        return _go("sources", "stay", anchor=anchor, err=_scrub(str(exc)[:1].upper() + str(exc)[1:] + ".", typed))
    return _go("sources", "stay", anchor=anchor, msg=f"{d['label']} saved.")


# ------------------------------------------------------------------- step 5 and 6, and the Today card

@router.get("/setup/connections", response_class=HTMLResponse)
def connections_page(request: Request):
    return _page(request, "setup_connections.html", "connections",
                 build=lambda conn, live, rows: {"rows": rows})


def _review_context(conn, live, rows):
    active = methodology.active()
    s = state.sources_state(conn)
    return {"p": seller.profile(), "method": active, "method_chosen": bool(config.load_user("methodology").get("active")),
            "provider": state.provider_state(conn), "provider_copy": PROVIDER_COPY, "sources_state": s,
            "rows": rows, "missing": state.missing(conn, live, rows),
            "style_is_own": (config.user_dir() / "style.md").exists(),
            "finished": bool(state.get_state(conn, state.FINISHED_KEY))}


@router.get("/setup/review", response_class=HTMLResponse)
def review_page(request: Request):
    return _page(request, "setup_review.html", "review", build=_review_context)


@router.post("/setup/finish")
def finish(request: Request):
    webapp = _web()
    if not seller.is_configured():
        return webapp._redirect("/setup/you", err="Fill in your profile first: the coach needs it to start.")
    with webapp._db(request) as conn:
        set_state(conn, state.FINISHED_KEY, now())
        conn.commit()
    return webapp._redirect("/", msg="You are set up. Everything here stays editable under Settings.")


@router.post("/setup/dismiss-card")
def dismiss_card(request: Request):
    webapp = _web()
    with webapp._db(request) as conn:
        set_user_state(conn, state.DISMISSED_KEY, state.card_signature(state.provider_state(conn)))
        conn.commit()
    return webapp._redirect("/")


def today_card(request: Request) -> dict | None:
    """For today.html (a template global, like the automation plugin's). Never raises into the page."""
    from ..store import stores
    try:
        conn = stores.sales(request.app.state.db_path)
    except Exception:
        return None
    try:
        return state.today_card(conn)
    except Exception:
        return None
    finally:
        conn.close()


def install_globals() -> None:
    _web().templates.env.globals["setup_today"] = today_card
