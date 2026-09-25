"""The rep's own recorder connections and their meetings (Phase 4; sources/connections.py).

  POST /me/recorders/{kind}/connect                  Test (the typed key, else the stored one) or Save a key
  POST /me/connections/{connection_id}/disconnect    delete the key and webhook secrets; polling stops
  POST /me/connections/{connection_id}/poll          "Import now": poll that connection at once
  POST /me/connections/{connection_id}/webhook-token a new per-connection webhook token, shown ONCE
  POST /me/connections/{connection_id}/signing-secret the recorder's webhook signing secret (Fathom, Granola)
  GET  /me/meetings                                  recent and upcoming meetings with their status; read-only
  POST /import/webhook/{connection_id}               the recorder's push (no session: the connection's owner)

Every /me route acts on the ACTING user's own connection only: a connection id that is not theirs is
404, exactly like one that does not exist. Keys are write-only: they arrive in a POST body, are
encrypted at once and never rendered, redirected, logged or flashed; error text is scrubbed of them.
Connections exist in a cloud install; on a local install the org-level sources under Setup stay the way
calls come in, and these routes send the local user there.
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import hosted, identity, seller
from ..execution import tokens
from . import connections
from .adapters import MAX_BYTES, SourceAuthError, SourceError

router = APIRouter()
SETUP = "/me/setup"
WEBHOOK_PREFIX = "/import/webhook/"


def _web():
    from ..web import app as webapp
    return webapp


def _cloud_only():
    """A local install has no per-rep connections: its sources are the org-level ones under Setup."""
    if not identity.cloud():
        return RedirectResponse("/setup/sources", status_code=303)
    return None


def _back(kind_or_id: str, msg=None, err=None):
    return _web()._redirect(SETUP, msg=msg, err=err, anchor="recorders")


def _scrub(text: str, *values) -> str:
    out = str(text or "")
    for value in values:
        if value and len(value) >= 4:
            out = out.replace(value, "[key]")
    return out[:400]


def webhook_url(request: Request, connection_id: str) -> str:
    base = hosted.public_origin() or str(request.base_url).rstrip("/")
    return f"{base}{WEBHOOK_PREFIX}{connection_id}"


@router.post("/me/recorders/{kind}/connect")
def recorder_connect(request: Request, kind: str, api_key: str = Form(""), action: str = Form("save")):
    refused = _cloud_only()
    if refused:
        return refused
    if kind not in connections.kinds():
        raise HTTPException(404, "no such recorder")
    typed = (api_key or "").strip()
    label = connections.kinds()[kind].label
    with _web()._db(request) as conn:
        try:
            if action == "test":
                account = connections.test_key(conn, kind, typed or None)
                who = f" Connected as {account.email}." if account.email else ""
                return _back(kind, msg=f"The {label} key works.{who}" + (" It is not saved yet: press Save." if typed else ""))
            if not typed:
                raise connections.ConnectionError_(f"Paste your {label} API key first.")
            try:
                account = connections.test_key(conn, kind, typed)
                note = ""
            except SourceAuthError:
                raise
            except SourceError as exc:          # the recorder is down or slow: keep the key, say so
                account, note = None, f" It could not be checked just now ({_scrub(exc, typed)}); the first poll will tell."
            connections.save_key(conn, kind, typed, account)
        except connections.ConnectionError_ as exc:
            return _back(kind, err=_scrub(exc, typed))
        except SourceAuthError:
            return _back(kind, err=f"{label} refused that key. Check it was copied whole, from your own account.")
        except SourceError as exc:
            return _back(kind, err=f"{label} could not be reached: {_scrub(exc, typed)}")
        except tokens.TokenError as exc:
            return _back(kind, err=f"This install cannot store keys yet ({_scrub(exc, typed)}). Ask your admin.")
    who = f" as {account.email}" if account and account.email else ""
    return _back(kind, msg=f"{label} connected{who}. New meetings come in at the next check, or press Import now.{note}")


def _mine(conn, connection_id: str) -> dict:
    row = connections.get(conn, connection_id)
    if row is None:
        raise HTTPException(404, "no such connection")
    return row


@router.post("/me/connections/{connection_id}/disconnect")
def recorder_disconnect(request: Request, connection_id: str):
    refused = _cloud_only()
    if refused:
        return refused
    with _web()._db(request) as conn:
        row = _mine(conn, connection_id)
        connections.disconnect(conn, row["id"])
    return _back(row["kind"], msg=f"{row.get('label') or row['kind']} disconnected; its key is deleted. "
                                  "The calls it brought in stay yours.")


@router.post("/me/connections/{connection_id}/poll")
def recorder_poll(request: Request, connection_id: str, back: str = Form("")):
    refused = _cloud_only()
    if refused:
        return refused
    with _web()._db(request) as conn:
        row = _mine(conn, connection_id)
        if row["status"] == "disconnected":
            return _back(row["kind"], err="That recorder is disconnected. Connect it again first.")
        if row["kind"] not in connections.allowed_kinds():
            return _back(row["kind"], err="Your admin has switched this recorder off for the org.")
        if row["status"] == "error":
            return _back(row["kind"], err=row.get("last_error") or "The recorder refused the key. Reconnect first.")
        result = connections.poll_user(conn, force=True, only=row["id"]).get(row["kind"]) or {}
    target = "/me/meetings" if back == "meetings" else None
    if "error" in result:
        msg, err = None, result["error"]
    else:
        n, held = result.get("imported", 0), result.get("needs_speaker", 0)
        msg = (f"Checked: {result.get('listed', 0)} recent meeting(s), {n} imported"
               + (f", {held} waiting for you to say which speaker you are" if held else "")
               + (f", {result.get('pending')} not transcribed yet" if result.get("pending") else "") + ".")
        err = None
    if target:
        return _web()._redirect(target, msg=msg, err=err)
    return _back(row["kind"], msg=msg, err=err)


@router.post("/me/connections/{connection_id}/webhook-token", response_class=HTMLResponse)
def recorder_webhook_token(request: Request, connection_id: str):
    refused = _cloud_only()
    if refused:
        return refused
    with _web()._db(request) as conn:
        row = _mine(conn, connection_id)
        token = connections.new_webhook_token(conn, row["id"])
    if token is None:
        return _back(row["kind"], err="Connect the recorder first.")
    from ..web import me
    # Rendered into this one response (no-store), never redirected with it: reloading cannot show it again.
    return me._page(request, seller.profile(), seller.style_guide(),
                    new_webhook={"kind": row["kind"], "label": row.get("label") or row["kind"],
                                 "url": webhook_url(request, row["id"]), "token": token})


@router.post("/me/connections/{connection_id}/signing-secret")
def recorder_signing_secret(request: Request, connection_id: str, secret: str = Form("")):
    refused = _cloud_only()
    if refused:
        return refused
    with _web()._db(request) as conn:
        row = _mine(conn, connection_id)
        try:
            ok = connections.set_signing_secret(conn, row["id"], secret)
        except connections.ConnectionError_ as exc:
            return _back(row["kind"], err=str(exc))
        except tokens.TokenError as exc:
            return _back(row["kind"], err=f"This install cannot store secrets yet ({_scrub(exc, secret)}).")
    if not ok:
        return _back(row["kind"], err="Connect the recorder first.")
    return _back(row["kind"], msg="Signing secret saved. Pushes signed by the recorder are accepted from now on.")


@router.get("/me/meetings", response_class=HTMLResponse)
def my_meetings(request: Request):
    webapp = _web()
    with webapp._db(request) as conn:
        data = connections.meetings(conn)
        mine = [connections.public(r) for r in connections.list_mine(conn)] if identity.cloud() else []
        response = webapp.render(request, conn, "me_meetings.html", upcoming=data["upcoming"], recent=data["recent"],
                                 recorders=[r for r in mine if r["status"] != "disconnected"],
                                 allowed=connections.allowed_kinds() if identity.cloud() else [])
    response.headers["Cache-Control"] = "no-store"
    return response


# ---- the push ------------------------------------------------------------------------------------

async def _read_capped(request: Request, limit: int):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/import/webhook/{connection_id}")
async def connection_webhook(request: Request, connection_id: str):
    """A recorder's push for ONE rep's connection. No session: the web layer lets exactly this path through
    (web/auth.is_open, web/app.SameOriginGuard) and this handler is the whole of its authentication. The
    owner is the connection's; nothing in the payload can name another."""
    if not identity.cloud():
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await _read_capped(request, MAX_BYTES)
    if body is None:
        return JSONResponse({"error": f"payload larger than {MAX_BYTES} bytes"}, status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    status, answer = await run_in_threadpool(connections.handle_webhook, request.app.state.db_path, connection_id,
                                             headers, body)
    return JSONResponse(answer, status_code=status)
