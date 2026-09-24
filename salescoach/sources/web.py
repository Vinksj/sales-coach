"""Web routes of the sources plugin.

  POST /import/file              upload a transcript file (any format parsers.parse_any reads)
  POST /import/webhook           a recorder's automation posts the generic JSON; shared secret
  POST /calls/{id}/speaker       the answer to "which speaker are you?" for a held import

/import/file and /calls/{id}/speaker are ordinary browser POSTs behind the same-origin guard.
/import/webhook is the one route a non-browser client may call: web/app.py lets a request past
the guard ONLY when it is a POST to exactly this path carrying the valid secret, and this handler
checks the secret again itself, so it holds with or without the guard in front of it. It does one
thing: import a transcript. It takes no deal id, no history flag and no instruction of any kind
from the payload; the deal is inferred from the participants' domains as for every other source.
"""
from urllib.parse import urlencode

import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import seller
from ..store import stores
from ..store import db
from . import base, settings
from .adapters import MAX_BYTES, SourceError, webhook
from .adapters.upload import UploadAdapter

router = APIRouter()
log = logging.getLogger("salescoach.sources")
LANGS = ("auto", "en", "hinglish")
ASK = "One question before this call is processed: which speaker are you?"


def _redirect(url, msg=None, err=None, anchor=None):
    params = {k: v[:400] for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    return RedirectResponse(url + (f"#{anchor}" if anchor else ""), status_code=303)


@router.post("/import/file")
def import_file_post(request: Request, file: UploadFile = File(...), title: str = Form(""), deal_id: str = Form(""),
                     lang_mode: str = Form("auto"), me_label: str = Form(""),
                     participants: list[str] = Form(default=[])):
    if not seller.is_configured():
        return _redirect("/setup", err=seller.NOT_CONFIGURED)
    data = file.file.read(MAX_BYTES + 1)
    conn = stores.sales(request.app.state.db_path)
    try:
        # What the form names must exist: an unknown id used to reach the INSERT and fail as a 500.
        if deal_id and conn.execute("SELECT 1 FROM deals WHERE node_id=?", (deal_id,)).fetchone() is None:
            raise HTTPException(404, "deal not found")
        people = list(dict.fromkeys(p for p in participants if p))
        for person_id in people:
            if conn.execute("SELECT 1 FROM people WHERE node_id=?", (person_id,)).fetchone() is None:
                raise HTTPException(404, "person not found")
        nt = UploadAdapter().normalize(data, file.filename, title.strip() or None)
        outcome = base.import_normalized(conn, nt, deal_id=deal_id or base.guess_deal(conn, nt),
                                         lang_mode=lang_mode if lang_mode in LANGS else "auto",
                                         me_label=me_label.strip() or None, participant_ids=people, add_me=True)
    except (ValueError, SourceError) as exc:          # UnrecognisedTranscript is a ValueError
        conn.rollback()
        return _redirect("/import", err=str(exc))
    finally:
        conn.close()
    if outcome.needs_speaker:
        return _redirect(f"/calls/{outcome.call_id}", msg=ASK, anchor="speaker")
    if not outcome.created:
        return _redirect(f"/calls/{outcome.call_id}", msg="This transcript was already imported; this is that call.")
    return _redirect(f"/calls/{outcome.call_id}", msg="Imported. Processing starts as soon as the worker is free.")


@router.post("/calls/{call_id}/speaker")
def call_speaker_post(request: Request, call_id: str, label: str = Form("")):
    conn = stores.sales(request.app.state.db_path)
    try:
        if conn.execute("SELECT 1 FROM calls WHERE node_id=?", (call_id,)).fetchone() is None:
            return JSONResponse({"error": "call not found"}, status_code=404)
        try:
            moved = base.resolve_speaker(conn, call_id, label or base.NOT_PRESENT, actor="user:ui")
        except ValueError as exc:
            conn.rollback()
            return _redirect(f"/calls/{call_id}", err=str(exc))
    finally:
        conn.close()
    said = f"{moved} turns are yours." if moved else "Nobody on this transcript is you."
    return _redirect(f"/calls/{call_id}", msg=f"{said} Processing starts as soon as the worker is free.")


async def _read_capped(request: Request, limit: int):
    """The body, or None when it is larger than `limit` (a missing or lying Content-Length included)."""
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


@router.post("/import/webhook")
async def import_webhook(request: Request):
    if not webhook.secret_configured():
        return JSONResponse({"error": "the webhook is off: no WEBHOOK_SECRET is set"}, status_code=403)
    if not webhook.authorised(request.headers.get(webhook.HEADER)):
        return JSONResponse({"error": "wrong or missing X-Salescoach-Secret"}, status_code=403)
    if not settings()["webhook"]["enabled"]:
        return JSONResponse({"error": "the webhook source is disabled in settings"}, status_code=403)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not webhook.reachable(request.client.host if request.client else None, headers):
        return JSONResponse({"error": webhook.REMOTE_REFUSED}, status_code=403)
    body = await _read_capped(request, MAX_BYTES)
    if body is None:
        return JSONResponse({"error": f"payload larger than {MAX_BYTES} bytes"}, status_code=413)
    # Off the event loop: parsing is CPU work, and one slow payload must not stall every page and stream.
    return await run_in_threadpool(_import_webhook_body, request.app.state.db_path, body)


def _import_webhook_body(db_path, body: bytes):
    conn = stores.sales(db_path)
    try:
        nt = webhook.WebhookAdapter().normalize(body)
        outcome = base.import_normalized(conn, nt, deal_id=base.guess_deal(conn, nt), history=False, add_me=True,
                                         link="account")
    except seller.NotConfigured as exc:
        conn.rollback()
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (ValueError, SourceError) as exc:
        conn.rollback()
        return JSONResponse({"error": str(exc)[:500]}, status_code=422)
    except db.OperationalError:
        conn.rollback()                                 # a locked store is OUR problem: say "try again", not "bad payload"
        return JSONResponse({"error": "the coach is busy; send it again in a moment"}, status_code=503)
    except Exception as exc:
        # A payload is somebody else's data: whatever shape it has, the answer is "not a transcript"
        # (422), never a 500 that an automation retries for ever. import_normalized took the raw file back.
        conn.rollback()
        log.warning("webhook payload refused: %s", type(exc).__name__)
        return JSONResponse({"error": f"the payload could not be read as a transcript ({type(exc).__name__})"},
                            status_code=422)
    finally:
        conn.close()
    return JSONResponse({"call_id": outcome.call_id, "created": outcome.created,
                         "needs_speaker": outcome.needs_speaker}, status_code=201 if outcome.created else 200)

