"""Local web UI for the sales coach.

What the seller does here: review every processed call, fix and approve the
follow-up email, confirm or reject what the agents extracted, track open
loops, and see his coaching patterns. Every page leads with the decision it
needs from him.

Rules this module keeps:
  * Server-rendered Jinja2 plus a little vanilla JS (static/app.js). No CDN,
    no external fonts or assets.
  * One sales.db connection per request, opened and closed inside the
    handler's own thread (sqlite handles are thread-bound). Background
    threads (the workflow worker, the live pipeline) open their own.
  * Same-origin guard: every non-GET request must carry an Origin or Referer
    on 127.0.0.1 or localhost, so no other web page can press Send or Confirm
    through the seller's browser. Hosted (SALESCOACH_PUBLIC_URL), the public
    URL takes localhost's place, and a login (web/auth.py) sits in front.
  * Mail leaves only through the Send and Save-to-Drafts handlers, and those
    only through execution.policy.approve_and_send (lint-gated, once-only).
    Nothing here ever sends on its own.
"""
import asyncio
import html
import io
import json
import logging
import threading
import queue
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import config, hosted, identity, ops, repo, seller
from ..execution import policy
from ..memory import patterns
from ..orchestrator import bus, context, review, workflow
from ..schemas.events import Event
from ..store import stores
from ..validators import evidence as evidence_v
from ..validators import recipients as recipients_v

HERE = Path(__file__).resolve().parent
ACTOR = "user"

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
KEEPALIVE_S = 15.0
SILENCE_ALERT_S = 60
CLIP_PAD_S = 0.4
CLIP_MAX_S = 120.0

OWNERS = ("me", "prospect", "mutual", "internal")
PRIORITIES = ("critical", "high", "medium", "low")
LOOP_TYPES = ("my_action", "prospect_action", "mutual", "follow_up", "info_request", "deal_risk")
LOOP_STATUSES = ("open", "waiting", "done", "cancelled", "superseded")
LANGS = ("auto", "en", "hinglish")
AUDIO_LAYOUTS = ("stereo_me_left", "mono_them")
READY_STATES = set(workflow.POST_PIPELINE)
class _Chain:
    """The live state order. Plugins insert pipeline steps after import, so this
    must read workflow.STEP_NAMES each time rather than copy it once."""

    def _items(self):
        return ["captured", *workflow.STEP_NAMES]

    def __contains__(self, item):
        return item in self._items()

    def __iter__(self):
        return iter(self._items())

    def __len__(self):
        return len(self._items())

    def __getitem__(self, i):
        return self._items()[i]

    def index(self, item):
        return self._items().index(item)


CHAIN = _Chain()
STEP_LABELS = {
    "live": "Live",
    "captured": "Audio captured",
    "final_transcribed": "Final transcript",
    "diarized": "Speakers separated",
    "quality_done": "Quality check",
    "summarized": "Summary",
    "analyzed": "Analysis",
    "actions_extracted": "Actions extracted",
    "loops_reconciled": "Loops reconciled",
    "email_drafted": "Email draft",
    "awaiting_review": "Ready for review",
    "reviewed": "Reviewed",
    "email_sent": "Email sent",
    "email_skipped": "Email skipped",
    "done": "Done",
    "capture_failed": "Capture failed",
    "needs_speaker": "Needs you: which speaker are you?",
}
PRIORITY_ORDER = "CASE {col} WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
_UNSET = object()


# ---- formatting ---------------------------------------------------------------

def today_ist() -> date:
    return datetime.now(seller.zone()).date()


def today_str() -> str:
    return today_ist().isoformat()


def _ist(raw):
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(seller.zone())


def fmt_dt(raw) -> str:
    ts = _ist(raw)
    if ts is None:
        return str(raw or "")
    label = f"{ts:%a} {ts.day} {ts:%b}, {ts:%H:%M}"
    return label if ts.year == today_ist().year else f"{ts.day} {ts:%b %Y}, {ts:%H:%M}"


def fmt_day(raw) -> str:
    if not raw:
        return ""
    try:
        d = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return str(raw)
    label = f"{d:%a} {d.day} {d:%b}"
    return label if d.year == today_ist().year else f"{label} {d.year}"


def mmss(seconds) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def fromjson(raw, default=None):
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def pct(x) -> int:
    return int(round((x or 0) * 100))


def step_label(name) -> str:
    name = name or ""
    return STEP_LABELS.get(name) or workflow.STEP_LABELS.get(name) or name.replace("_", " ")


def human(s) -> str:
    return seller.display(s).replace("_", " ")


def pretty_json(raw) -> str:
    if raw is None:
        return ""
    try:
        return json.dumps(json.loads(raw) if isinstance(raw, str) else raw, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)


templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters.update(dt=fmt_dt, day=fmt_day, mmss=mmss, fromjson=fromjson, pct=pct,
                             step_label=step_label, human=human, pretty=pretty_json,
                             who=seller.display)
# Called from templates on every render, so a saved profile shows at once: {{ brand() }}, {{ tz_label() }}.
templates.env.globals.update(brand=seller.company, tz_label=seller.tz_label, languages=seller.languages,
                             settings_problems=config.user_problems, auth_on=hosted.auth_enabled, hosted=hosted.is_hosted,
                             me_user_id=lambda: getattr(identity.current_actor(required=False), "user_id", None))


# ---- same-origin guard --------------------------------------------------------

class SameOriginGuard:
    """Refuse any state-changing request that did not come from this app's own pages.

    Two modes. On the localhost install (no SALESCOACH_PUBLIC_URL) the app IS the origin: Host must
    be loopback, a forwarded request is refused, Origin must be this host. Hosted (a public URL is
    set) the platform's proxy is the only way in, so every request carries forwarding headers; then
    Host and Origin are checked against the public URL itself (scheme, host and port)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            host = headers.get("host", "")
            webhook_call = self._webhook_call(scope, headers)
            if webhook_call == "open":
                # The ONE non-browser door: a recorder's automation posting a transcript. It is let past
                # the Host and Origin checks only for POST to exactly /import/webhook AND only when it
                # carries the configured shared secret (constant-time compare; no secret set = never).
                # A browser page cannot know the secret, so this opens nothing to cross-site requests;
                # without it the request falls through to the checks below like any other.
                # Unless the user switched on "allow requests through a tunnel", the request must also be
                # provably local (webhook.reachable): loopback peer, local Host, no forwarding header.
                await self.app(scope, receive, send)
                return
            if webhook_call == "remote":
                # The right secret from somewhere the user has not allowed: say so, in the webhook's own words,
                # instead of the browser refusal below, which would send the deployer looking at the wrong thing.
                from ..sources.adapters import webhook
                await JSONResponse({"error": webhook.REMOTE_REFUSED}, status_code=403)(scope, receive, send)
                return
            proxied = hosted.proxy_mode()
            # Review 3: a tunnel or proxy that REWRITES the Host header makes every request look local, and a
            # non-browser client can then forge Origin too. Tunnels and proxies announce themselves with a
            # forwarding header; a browser on this machine never sends one. Nothing but the webhook (above,
            # when the user allowed it) is served to a forwarded request. Hosted, the platform is the proxy
            # and every request is forwarded: there the public URL below is what Host and Origin must match.
            if not proxied and self._forwarded(headers):
                await PlainTextResponse("Refused: this request came through a proxy or a tunnel. Only the "
                                        "transcript webhook may be reached that way.", status_code=403)(scope, receive, send)
                return
            # DNS rebinding: a foreign page whose name resolves to 127.0.0.1 still sends its own Host.
            if not self._host_ok(host, proxied):
                await PlainTextResponse("Refused: unknown host.", status_code=403)(scope, receive, send)
                return
            if scope["method"].upper() not in SAFE_METHODS:
                # The exact origin (scheme, host AND port) must be this server. Review 2026-09-12: a page on
                # another localhost port passed a hostname-only check and could press Send.
                source = headers.get("origin") or headers.get("referer") or ""
                parsed = urlparse(source) if source and source != "null" else None
                origin = f"{parsed.scheme}://{parsed.netloc}" if parsed and parsed.netloc else None
                if proxied:
                    own, origin = {hosted.public_origin()}, (origin.lower() if origin else None)
                else:
                    own = {f"http://{host}", f"https://{host}"}
                if origin not in (own | self._trusted_origins(scope)):
                    response = PlainTextResponse("Refused: this request did not come from the sales coach itself.",
                                                 status_code=403)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)

    def _host_ok(self, host: str, proxied: bool) -> bool:
        """Localhost: a loopback name. Hosted: the public URL's host (and port), or a loopback name, which
        is how the container's own health check reaches the server."""
        if proxied and hosted.host_matches_public(host):
            return True
        try:
            return urlparse(f"//{host}").hostname in self._allowed_hosts()
        except ValueError:
            return False

    @staticmethod
    def _webhook_call(scope, headers) -> str:
        """"open" for an authorised webhook call from where the user allows, "remote" for an authorised one
        from elsewhere, "" for anything else (a browser request, or a wrong or missing secret)."""
        from ..sources.adapters import webhook
        if scope["method"].upper() != "POST" or scope.get("path") != webhook.PATH:
            return ""
        if not webhook.authorised(headers.get(webhook.HEADER)):
            return ""
        client = scope.get("client")
        return "open" if webhook.reachable(client[0] if client else None, headers) else "remote"

    @staticmethod
    def _forwarded(headers) -> bool:
        from ..sources.adapters import webhook
        return webhook.forwarded(headers)

    def _allowed_hosts(self):
        return ALLOWED_HOSTS | {"testserver"}          # testserver: FastAPI's TestClient only

    @staticmethod
    def _trusted_origins(scope):
        app = scope.get("app")
        return getattr(getattr(app, "state", None), "trusted_origins", set()) if app else set()


# ---- first run ----------------------------------------------------------------

NOT_CONFIGURED = "Set up your profile first: the coach needs to know who is selling what before it can take a call."
# GETs that are not pages: event streams, JSON for the overlay and the page scripts, audio clips.
_NOT_A_PAGE = re.compile(r"(\.json|/events|/stream|/nudges|/clip)$")


class FirstRunGate:
    """Until the profile exists, every page leads to the setup that is missing: /setup while the ORG
    half (the company, what it sells) is not there, /me/setup while the acting USER's half (a name,
    an address) is not. Streams, JSON and static files pass (they are fetched BY pages), and so does
    every POST: the routes that would create a call refuse on their own, with a message, instead of
    bouncing a form post."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"].upper() in ("GET", "HEAD"):
            path = scope.get("path") or "/"
            exempt = (path in ("/setup", "/health", "/login", "/me/setup") or path.startswith(("/setup/", "/static/"))
                      or _NOT_A_PAGE.search(path))
            if not exempt:
                if not seller.org_configured():
                    await RedirectResponse("/setup", status_code=303)(scope, receive, send)
                    return
                if not seller.user_configured():
                    await RedirectResponse("/me/setup", status_code=303)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class ActorGate:
    """Binds the acting user for the request (identity.activate), so every route handler, Jinja
    filter and store connection opened inside runs as that user. Local mode: the local user, always.
    Cloud mode: the user named by the login cookie (hosted.verify_session), looked up in `users`;
    a request that resolves to no active user gets 401 (open paths pass with no actor). Phase 3
    replaces the lookup with Google sign-in and server-side sessions; the binding stays."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        actor = identity.LOCAL_ACTOR if not identity.cloud() else self._cloud_actor(scope)
        if actor is None and identity.cloud():
            from . import auth
            if not auth.is_open(scope["method"].upper(), scope.get("path") or "/"):
                await JSONResponse({"error": "no active user for this session"}, status_code=401)(scope, receive, send)
                return
        with identity.activate(actor):
            await self.app(scope, receive, send)

    @staticmethod
    def _cloud_actor(scope):
        from . import auth
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        session = hosted.verify_session(auth._cookie(headers))
        if not session:
            return None
        from .. import users
        try:
            with identity.activate(None):
                conn = stores.sales(scope["app"].state.db_path)
            try:
                row = users.get(conn, session["user"]) or users.by_email(conn, session["user"])
            finally:
                conn.close()
        except Exception:
            return None
        if not row or row["status"] != "active":
            return None
        return identity.Actor(row["id"], identity.INTERACTIVE, row["role"], users.profile_of(row))


# ---- plumbing -----------------------------------------------------------------

@contextmanager
def _db(request: Request):
    conn = stores.sales(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _default_live_factory():
    if identity.cloud():                     # no microphone, no MLX: live capture is a local-install feature
        return None
    try:
        from ..live.manager import get_manager
    except Exception:
        return None
    return get_manager


def _default_hub():
    try:
        from ..live import hub as hub_module
    except Exception:
        return None
    return hub_module.hub


def _default_gmail_factory():
    from ..execution.gmail import GmailProvider
    alias = (config.load("policy").get("email") or {}).get("account", "work")
    return GmailProvider(alias)


def _audio_importer():
    try:
        from ..sources.audio_file import import_audio
    except Exception:
        return None
    return import_audio


def _live_manager(app):
    factory = app.state.live_factory
    if factory is None:
        return None
    try:
        return factory()
    except Exception:
        return None


def _live_status(app) -> dict:
    mgr = _live_manager(app)
    if mgr is None:
        return {"available": False, "active": False}
    try:
        status = dict(mgr.status())
    except Exception as exc:
        return {"available": True, "active": False, "error": f"{type(exc).__name__}: {exc}"}
    status["available"] = True
    status.setdefault("active", False)
    return status


def worker_on(app, conn=None) -> bool:
    """Is anything going to handle the queue? The worker thread in this process, or, when this process
    is the `web` role, a worker process with a fresh heartbeat (ops.read_heartbeats)."""
    thread = getattr(app.state, "worker_thread", None)
    if thread is not None and thread.is_alive():
        return True
    if getattr(app.state, "role", "all") == "web" and conn is not None:
        try:
            return any(not b["stale"] for b in ops.read_heartbeats(conn)["workers"])
        except Exception:
            return False
    return False


def _chrome(request: Request, conn) -> dict:
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM wf_events WHERE status!='done' GROUP BY status")}
    worker = request.app.state.worker
    current = getattr(worker, "current", None) if worker is not None else None
    return {
        "path": request.url.path,
        "nav_queued": counts.get("pending", 0) + counts.get("running", 0),
        "nav_failed": counts.get("failed", 0),
        "nav_worker_on": worker_on(request.app, conn),
        "nav_current": f"{current.type} {current.entity_id or ''}".strip() if current else None,
        "live": _live_status(request.app),
        "flash_msg": request.query_params.get("msg"),
        "flash_err": request.query_params.get("err"),
        "today": today_str(),
        "step_names": workflow.STEP_NAMES,
    }


def render(request: Request, conn, name: str, **ctx):
    return templates.TemplateResponse(request, name, {**_chrome(request, conn), **ctx})


def _clean_next(nxt, default: str) -> str:
    """A same-app path to go back to, never an open redirect; drops any old flash."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return default
    u = urlparse(nxt)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in ("msg", "err")]
    return u.path + (("?" + urlencode(q)) if q else "") + (("#" + u.fragment) if u.fragment else "")


def _redirect(url: str, msg=None, err=None, anchor=None):
    frag = ""
    if "#" in url:
        url, frag = url.split("#", 1)
    if anchor:
        frag = anchor
    params = {k: v[:400] for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    if frag:
        url += "#" + frag
    return RedirectResponse(url, status_code=303)


def _one(conn, sql, params, what):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


def _call_or_404(conn, call_id):
    return _one(conn, "SELECT * FROM calls WHERE node_id=?", (call_id,), f"call {call_id}")


def _loop_or_404(conn, loop_id):
    return _one(conn, "SELECT * FROM loops WHERE node_id=?", (loop_id,), f"loop {loop_id}")


def _email_or_404(conn, email_id):
    return _one(conn, "SELECT * FROM emails WHERE id=?", (email_id,), f"email {email_id}")


def _deal_or_404(conn, deal_id):
    return _one(conn, "SELECT d.*, a.name AS account_name, a.domains FROM deals d "
                      "LEFT JOIN accounts a ON a.node_id=d.account_id WHERE d.node_id=?", (deal_id,), "deal")


def _deals(conn):
    return conn.execute("SELECT d.node_id, d.name, d.status, a.name AS account_name FROM deals d "
                        "LEFT JOIN accounts a ON a.node_id=d.account_id "
                        "ORDER BY d.status='active' DESC, d.name").fetchall()


def _people(conn):
    return conn.execute("SELECT p.*, a.name AS account_name FROM people p "
                        "LEFT JOIN accounts a ON a.node_id=p.account_id WHERE p.is_me=0 ORDER BY p.name").fetchall()


def _audio_files(call) -> list:
    if not call["audio_dir"]:
        return []
    d = Path(call["audio_dir"])
    return [d / n for n in ("me.flac", "them.flac") if (d / n).is_file()]


def _latest_email(conn, call_id):
    return conn.execute("SELECT * FROM emails WHERE call_id=? ORDER BY id DESC LIMIT 1", (call_id,)).fetchone()


def _call_job(conn, call_id):
    """The newest unfinished pipeline job for a call, if any."""
    return conn.execute(
        "SELECT * FROM wf_events WHERE entity_id=? AND type IN ('CALL_ENDED','PROCESS_CALL') AND status!='done' "
        "ORDER BY id DESC LIMIT 1", (call_id,)).fetchone()


def _next_step(state):
    if state in CHAIN and CHAIN.index(state) + 1 < len(CHAIN):
        return CHAIN[CHAIN.index(state) + 1]
    return None


def _failed_step(call) -> str:
    """The step to retry from: the one named in wf_error, else the one after wf_state."""
    head = (call["wf_error"] or "").split(":", 1)[0].strip()
    if head in workflow.STEP_NAMES:
        return head
    state = call["wf_state"]
    if state in CHAIN:
        return workflow.STEP_NAMES[min(CHAIN.index(state), len(workflow.STEP_NAMES) - 1)]
    return workflow.STEP_NAMES[0]


def _is_failed(call) -> bool:
    return bool(call["wf_error"]) or call["wf_state"] == "capture_failed"


def _is_processing(call) -> bool:
    return call["wf_state"] in CHAIN and not call["wf_error"]


def _publish_process(conn, call_id, step, force=False) -> bool:
    # The step is in the key so a double click dedupes but Redraft then Retry in the same second does not.
    return bus.publish(conn, Event(type="PROCESS_CALL", entity_id=call_id,
                                   dedupe_key=f"PROCESS:{identity.actor_of(conn).user_id}:{call_id}:{step}:{stores.now()}",
                                   payload={"from": step, "force": bool(force)}),
                       priority=bus.PRIORITY_INTERACTIVE)   # a redraft or a retry: someone is waiting


def _parse_addrs(raw) -> list:
    return [a.strip() for a in re.split(r"[,;\s]+", raw or "") if a.strip()]


def _norm_text(raw) -> str:
    return (raw or "").replace("\r\n", "\n").replace("\r", "\n")


def _speaker(turn, names) -> str:
    if turn["channel"] == "me":
        return "Me"
    if turn.get("person_id") and turn["person_id"] in names:
        return names[turn["person_id"]]
    cluster = turn.get("speaker_cluster")
    if cluster and cluster not in ("them", "them_1"):
        return cluster.replace("_", " ")
    return "Them"


def _find_or_create_person(conn, name, email, title=None, fallback_account=None):
    email = (email or "").strip().lower() or None
    if email and not recipients_v.EMAIL_RE.match(email):
        raise ValueError(f"not an email address: {email}")
    if email:
        existing = repo.find_person_by_email(conn, email)
        if existing:
            return existing
    name = (name or "").strip()
    if not name:
        raise ValueError("a new person needs a name")
    account = repo.find_account_by_domain(conn, email.split("@", 1)[1]) if email else None
    if account is None and not email:
        account = fallback_account
    return repo.create_person(conn, name, email=email, account_id=account, title=(title or "").strip() or None,
                              actor=ACTOR)


def _run_view(r) -> dict:
    run = dict(r)
    run["input_refs_p"] = pretty_json(run.get("input_refs"))
    run["created"] = fromjson(run.get("created_items"), []) or []
    run["rejected"] = fromjson(run.get("rejected_items"), []) or []
    run["output_p"] = pretty_json(run.get("output"))
    return run


# ---- clip audio ---------------------------------------------------------------

def _mix_clip(paths, start: float, end: float) -> bytes:
    """Both channels of [start, end) mixed to one mono 16-bit WAV."""
    import numpy as np
    import soundfile as sf

    tracks, rate = [], None
    for path in paths:
        info = sf.info(str(path))
        if rate is None:
            rate = info.samplerate
        elif info.samplerate != rate:
            continue
        a, b = int(start * rate), min(int(end * rate), info.frames)
        if a >= b:
            continue
        data, _ = sf.read(str(path), start=a, stop=b, dtype="float32", always_2d=True)
        tracks.append(data.mean(axis=1))
    if not tracks:
        raise LookupError("no audio in that time range")
    mix = np.zeros(max(len(t) for t in tracks), dtype=np.float32)
    for t in tracks:
        mix[:len(t)] += t
    peak = float(np.max(np.abs(mix))) if len(mix) else 0.0
    if peak > 1.0:
        mix /= peak
    buf = io.BytesIO()
    sf.write(buf, mix, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# ---- app ----------------------------------------------------------------------

router = APIRouter()


JARVIS_SYNC_S = 6 * 3600


def _background_duties(app, stop):
    """Startup recovery plus the periodic Jarvis sync (mirror, keep-alive, read-back, call claims)."""
    import logging
    import threading
    log = logging.getLogger("salescoach.web")
    try:
        from ..live.manager import get_manager
        get_manager().recover_orphans()         # finalize audio of calls a crash left 'live'
    except Exception:
        log.exception("orphan recovery failed")

    def jarvis_loop():
        from ..integrations import jarvis_bridge
        delay = 30
        while not stop.wait(delay):
            try:
                with identity.session(identity.LOCAL_USER, mode=identity.SERVICE, db_path=app.state.db_path) as conn:
                    jarvis_bridge.sync(conn)
                    conn.commit()
            except Exception:
                log.exception("jarvis sync failed")
            delay = JARVIS_SYNC_S

    if not identity.cloud():                 # the Jarvis bridge is the local user's; nothing to mirror in cloud
        threading.Thread(target=jarvis_loop, name="salescoach-jarvis-sync", daemon=True).start()
    from .. import plugins
    plugins.start_background(app.state.db_path, stop)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Role `all` (start_worker=True): the worker thread, the duties and the heartbeats live here. Role
    `web`, or start_worker=False: HTTP only; a worker and a scheduler process do the rest (ops.py)."""
    import threading
    worker, thread = None, None
    stop = threading.Event()
    if app.state.start_worker:
        from ..orchestrator.worker import Worker
        worker = Worker(live_busy=lambda: bool(_live_status(app).get("active")), db_path=app.state.db_path)
        thread = threading.Thread(target=worker.run, name="salescoach-worker", daemon=True)
        thread.start()
        _background_duties(app, stop)
        if app.state.heartbeats:
            _heartbeats(app, worker, stop)
    app.state.worker, app.state.worker_thread = worker, thread
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            worker.stop()
            thread.join(timeout=5)
        app.state.worker, app.state.worker_thread = None, None


def _heartbeats(app, worker, stop):
    """The single-process deploy says it is alive the same way the split roles do, so /health and
    `salescoach health` read one shape whatever the topology."""
    ops.Heartbeat(app.state.db_path, "worker", stop,
                  fields=lambda: {"concurrency": 1, "handled": worker.handled, "busy": int(worker.current is not None)}).start()

    def leader():
        conn = None
        while not stop.is_set():
            try:
                if conn is None:
                    with identity.activate(None):
                        conn = stores.sales(app.state.db_path)
                ops._write_leader(conn, app.state.started_at)
                ops.write_heartbeat(conn, "scheduler", started_at=app.state.started_at, leader=True)
            except Exception:
                logging.getLogger("salescoach.web").exception("scheduler heartbeat failed")
                conn = None
            if stop.wait(ops.HEARTBEAT_S):
                break
        if conn is not None:
            conn.close()
    threading.Thread(target=leader, name="salescoach-heartbeat-scheduler", daemon=True).start()


async def _http_error(request: Request, exc: StarletteHTTPException):
    if "text/html" not in request.headers.get("accept", ""):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    body = (f'<!doctype html><meta charset="utf-8"><title>{exc.status_code}</title>'
            f'<link rel="stylesheet" href="/static/app.css"><main class="wrap"><div class="page-head">'
            f'<div class="eyebrow">{exc.status_code}</div><h1>{html.escape(str(exc.detail))}</h1>'
            f'<p><a href="/">Back to Today</a></p></div></main>')
    return HTMLResponse(body, status_code=exc.status_code)


def create_app(start_worker: bool = True, db_path=None, gmail_factory=None, live_factory=_UNSET,
               hub=_UNSET, trusted_origins=None, role: str = "all", heartbeats: bool = False) -> FastAPI:
    """The web app. Tests pass start_worker=False and inject gmail_factory / live_factory / hub.

    start_worker=False also skips every background duty (scheduler, Jarvis sync), which is how
    `serve --no-worker` previews a database copy without anything acting on it. role="web" does the
    same for the split deploy (ops.py): this process serves HTTP and nothing else, whatever
    start_worker says. heartbeats=True (serve, role all) writes the worker/scheduler heartbeats.
    """
    if role not in ("all", "web"):
        raise ValueError(f"create_app serves HTTP: role must be all or web, not {role!r}")
    app = FastAPI(title="Sales Coach", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.role = role
    app.state.start_worker = start_worker and role == "all"
    app.state.heartbeats = heartbeats
    app.state.started_at = stores.now()
    # Tests post from a fixed origin to TestClient's "testserver" host; a real server trusts only itself.
    if trusted_origins is None:
        trusted_origins = set() if start_worker else {"http://127.0.0.1:8140", "http://localhost:8140"}
    app.state.trusted_origins = set(trusted_origins)
    app.state.db_path = db_path
    app.state.worker = None
    app.state.gmail_factory = gmail_factory or _default_gmail_factory
    app.state.live_factory = _default_live_factory() if live_factory is _UNSET else live_factory
    app.state.hub = _default_hub() if hub is _UNSET else hub
    app.state.sse_keepalive_s = KEEPALIVE_S
    app.state.login_limiter = hosted.LoginLimiter()
    from . import auth
    app.add_middleware(FirstRunGate)
    app.add_middleware(ActorGate)                   # outside the gate: "is this USER set up" needs to know who
    app.add_middleware(auth.AuthGate)               # inert without SALESCOACH_PASSWORD; else a session before any page
    app.add_middleware(SameOriginGuard)             # added last = outermost: the gate never sees a foreign request
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    from . import me
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(router)
    from . import setup as setup_pages
    app.include_router(setup_pages.router)
    from .. import plugins
    for plugin_router in plugins.routers():
        app.include_router(plugin_router)
    templates.env.globals["nav_ext"] = plugins.nav()
    templates.env.globals["step_labels"] = workflow.STEP_LABELS
    app.state.plugin_errors = dict(plugins.errors)
    workflow.ensure_plugins()
    app.add_exception_handler(StarletteHTTPException, _http_error)
    return app


# ---- health -------------------------------------------------------------------

def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("salescoach")
    except PackageNotFoundError:
        return "unknown"


@router.get("/health")
def health(request: Request):
    """For the platform's health check: open (no session, no origin needed beyond a valid Host), cheap
    (one SELECT 1 on the store), and it names no secret. 503 when the store cannot answer."""
    try:
        with _db(request) as conn:
            conn.execute("SELECT 1").fetchone()
        db_state = "ok"
    except Exception as exc:                        # a broken volume is exactly what this must report
        db_state = f"error: {type(exc).__name__}"
    thread = getattr(request.app.state, "worker_thread", None)
    processes = {"workers": [], "schedulers": [], "leader": None}
    if db_state == "ok":
        try:
            with _db(request) as conn:
                processes = ops.read_heartbeats(conn)
        except Exception:                           # the store answered SELECT 1 a moment ago; do not 503 on this
            pass
    body = {"status": "ok" if db_state == "ok" else "error", "version": _version(), "db": db_state,
            "role": getattr(request.app.state, "role", "all"),
            "worker": "running" if thread is not None and thread.is_alive() else "off",
            "processes": processes,
            "configured": bool(seller.is_configured())}
    return JSONResponse(body, status_code=200 if db_state == "ok" else 503)


# ---- Today --------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def today_page(request: Request):
    today = today_str()
    with _db(request) as conn:
        calls = conn.execute(
            "SELECT c.*, d.name AS deal_name FROM calls c LEFT JOIN deals d ON d.node_id=c.deal_id "
            "ORDER BY c.started_at DESC LIMIT 300").fetchall()
        awaiting, processing, failed, live_calls, held = [], [], [], [], []
        for c in calls:
            cid = c["node_id"]
            if c["wf_state"] in workflow.HOLD_STATES:
                held.append(c)                  # an import waiting for "which speaker are you?"
            elif _is_failed(c):
                failed.append({"call": c, "step": _failed_step(c), "job": _call_job(conn, cid),
                               "retryable": c["wf_state"] != "capture_failed"})
            elif c["wf_state"] == "live":
                live_calls.append(c)
            elif _is_processing(c):
                processing.append({"call": c, "next": _next_step(c["wf_state"]), "job": _call_job(conn, cid)})
            elif c["wf_state"] in ("awaiting_review", "reviewed"):
                email = _latest_email(conn, cid)
                if c["wf_state"] == "reviewed" and not (email and email["status"] in
                                                         ("drafted", "failed", "saved_to_gmail", "sending")):
                    continue
                analysis, _ = context.artifact(conn, cid, "analysis")
                awaiting.append({
                    "call": c, "email": email, "verdict": (analysis or {}).get("verdict"),
                    "proposed": conn.execute("SELECT COUNT(*) FROM loops WHERE call_id=? AND review_state='proposed'",
                                             (cid,)).fetchone()[0],
                    "conflicts": len(_open_conflicts(conn, cid, c["deal_id"])),
                })
        due = conn.execute(
            "SELECT l.*, d.name AS deal_name, c.title AS call_title FROM loops l "
            "LEFT JOIN deals d ON d.node_id=l.deal_id LEFT JOIN calls c ON c.node_id=l.call_id "
            "WHERE l.status IN ('open','waiting') AND l.review_state!='rejected' "
            "AND ((l.due_date IS NOT NULL AND l.due_date<=?) OR (l.next_check_at IS NOT NULL AND l.next_check_at<=?)) "
            f"ORDER BY COALESCE(l.due_date, l.next_check_at), {PRIORITY_ORDER.format(col='l.priority')}",
            (today, today)).fetchall()
        failed_ids = {f["call"]["node_id"] for f in failed}
        failed_jobs = [e for e in conn.execute(
            "SELECT * FROM wf_events WHERE status='failed' ORDER BY id DESC LIMIT 20").fetchall()
            if e["entity_id"] not in failed_ids]
        return render(request, conn, "today.html", awaiting=awaiting, processing=processing, failed=failed,
                      live_calls=live_calls, held=held, due=due, failed_jobs=failed_jobs,
                      priority=patterns.active_priority(conn), deals=_deals(conn), people=_people(conn),
                      today_label=fmt_day(today))


@router.post("/events/{event_id}/retry")
def event_retry(request: Request, event_id: int):
    with _db(request) as conn:
        _one(conn, "SELECT id FROM wf_events WHERE id=?", (event_id,), "event")
        conn.execute("UPDATE wf_events SET status='pending', attempts=0, error=NULL, updated_at=? WHERE id=?",
                     (stores.now(), event_id))
        conn.commit()
    return _redirect("/", msg="Queued again.")


# ---- live ---------------------------------------------------------------------

@router.post("/live/start")
def live_start(request: Request, title: str = Form(""), deal_id: str = Form(""), lang_mode: str = Form("auto"),
               participants: list[str] = Form(default=[])):
    if not seller.is_configured():
        return _redirect("/setup", err=NOT_CONFIGURED)
    mgr = _live_manager(request.app)
    if mgr is None:
        return _redirect("/", err="Live capture is not available in this server (the live module did not load).")
    lang = lang_mode if lang_mode in LANGS else "auto"
    with _db(request) as conn:
        me = repo.ensure_me(conn)
        conn.commit()
    people = list(dict.fromkeys([me, *[p for p in participants if p]]))
    try:
        call_id = mgr.start_call(title.strip() or "Untitled call", deal_id=deal_id or None, lang_mode=lang,
                                 participants=people)
    except Exception as exc:
        status = _live_status(request.app)
        if type(exc).__name__ == "LiveCallActive" and status.get("call_id"):
            return _redirect(f"/live/{status['call_id']}", err="A call is already live. Stop it before starting another.")
        return _redirect("/", err=f"Could not start capture: {type(exc).__name__}: {exc}")
    return _redirect(f"/live/{call_id}")


@router.get("/live/{call_id}", response_class=HTMLResponse)
def live_page(request: Request, call_id: str):
    with _db(request) as conn:
        call = _call_or_404(conn, call_id)
        status = _live_status(request.app)
        is_live = bool(status.get("active")) and status.get("call_id") == call_id
        deal, loops, prev_next = None, [], None
        if call["deal_id"]:
            deal = conn.execute("SELECT d.*, a.name AS account_name FROM deals d "
                                "LEFT JOIN accounts a ON a.node_id=d.account_id WHERE d.node_id=?",
                                (call["deal_id"],)).fetchone()
            loops = conn.execute(
                "SELECT * FROM loops WHERE deal_id=? AND status IN ('open','waiting') AND review_state!='rejected' "
                f"ORDER BY due_date IS NULL, due_date, {PRIORITY_ORDER.format(col='priority')}",
                (call["deal_id"],)).fetchall()
            for prev in conn.execute(
                    "SELECT node_id, title, started_at FROM calls WHERE deal_id=? AND node_id!=? "
                    "ORDER BY started_at DESC LIMIT 10", (call["deal_id"], call_id)):
                summary, _ = context.artifact(conn, prev["node_id"], "summary")
                if summary and summary.get("next_step"):
                    prev_next = {"text": summary["next_step"].get("text"), "call": prev}
                    break
        return render(request, conn, "live.html", call=call, is_live=is_live, status=status,
                      levels=(status.get("levels") or {}) if is_live else {},
                      orphan=call["wf_state"] == "live" and not is_live,
                      turns=repo.turns(conn, call_id, "live"), deal=deal, loops=loops, prev_next=prev_next,
                      participants=repo.call_participants(conn, call_id), silence_s=SILENCE_ALERT_S)


@router.post("/live/{call_id}/stop")
def live_stop(request: Request, call_id: str):
    mgr = _live_manager(request.app)
    if mgr is None:
        return _redirect(f"/live/{call_id}", err="Live capture is not available in this server.")
    try:
        mgr.stop_call(call_id)
    except Exception as exc:
        return _redirect(f"/live/{call_id}", err=f"Could not stop: {type(exc).__name__}: {exc}")
    return _redirect(f"/calls/{call_id}", msg="Call ended. Processing starts as soon as the worker is free.")


def _sse(message: dict) -> str:
    return "data: " + json.dumps(message, default=str) + "\n\n"


@router.get("/live/{call_id}/events")
async def live_events(request: Request, call_id: str):
    hub = request.app.state.hub
    if hub is None:
        raise HTTPException(503, "live hub not available")
    topic = f"call:{call_id}"
    q = hub.subscribe(topic)
    keepalive = request.app.state.sse_keepalive_s

    async def stream():
        try:
            yield "retry: 3000\n\n"
            latest = hub.latest(topic)
            for kind in ("status", "level", "alert", "ended"):
                if kind in latest:
                    yield _sse(latest[kind])
            if "ended" in latest:
                return
            last = time.monotonic()
            while True:
                try:
                    message = q.get_nowait()
                except queue.Empty:
                    if time.monotonic() - last >= keepalive:
                        yield ": keepalive\n\n"
                        last = time.monotonic()
                    await asyncio.sleep(0.1)
                    continue
                yield _sse(message)
                last = time.monotonic()
                if message.get("type") == "ended":
                    return
        finally:
            hub.unsubscribe(topic, q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- call review --------------------------------------------------------------

def _open_conflicts(conn, call_id, deal_id) -> list:
    rows = conn.execute(
        "SELECT mc.*, l.description AS loop_description, l.call_id AS loop_call FROM memory_conflicts mc "
        "LEFT JOIN loops l ON l.node_id=mc.entity_id WHERE mc.status='open' AND ("
        "l.call_id=? OR (CAST(? AS TEXT) IS NOT NULL AND l.deal_id=?) OR mc.entity_id=? OR mc.provenance LIKE ?) "
        "ORDER BY mc.id",
        (call_id, deal_id, deal_id, deal_id or call_id, f'%"{call_id}"%')).fetchall()
    out = []
    for r in rows:
        c = dict(r)
        prov = fromjson(c["provenance"], {}) or {}
        c["reason"] = prov.get("reason")
        c["evidence"] = prov.get("turns") or []
        c["source_call"] = prov.get("ref") if prov.get("kind") == "call" else None
        c["label"] = c["loop_description"] or c["entity_id"]
        c["field_label"] = c["field"].split(".", 1)[-1].replace("_", " ")
        out.append(c)
    return out


def _email_view(conn, email) -> dict:
    to, cc = fromjson(email["to_addrs"], []) or [], fromjson(email["cc_addrs"], []) or []
    allowed = recipients_v.allowed_recipients(conn, email["call_id"], email["deal_id"])
    user_added = [a for a in to + cc if a.lower() not in allowed]
    editable = email["status"] in ("drafted", "failed")
    decision = policy.evaluate(conn, email, user_added) if editable else None
    issues = decision.issues if decision else (fromjson(email["lint"], []) or [])
    return {"row": email, "to": to, "cc": cc, "allowed": allowed, "user_added": user_added, "decision": decision,
            "editable": editable, "blocks": [i for i in issues if i.get("severity") == "block"],
            "warns": [i for i in issues if i.get("severity") != "block"]}


def _speaker_question(conn, call_id) -> list:
    from ..sources import base
    return base.speaker_question(conn, call_id)


def _call_context(conn, call) -> dict:
    cid, state = call["node_id"], call["wf_state"]
    arts = {k: context.artifact(conn, cid, k) for k in ("quality", "summary", "analysis", "actions", "reconcile", "email")}
    run_ids = {k: (row["run_id"] if row is not None else None) for k, (_, row) in arts.items()}
    quality, summary, analysis = arts["quality"][0], arts["summary"][0], arts["analysis"][0]
    actions_art, reconcile = arts["actions"][0] or {}, arts["reconcile"][0] or {}
    email_art = arts["email"][0]

    audio = _audio_files(call)
    names = {r["node_id"]: r["name"] for r in conn.execute("SELECT node_id, name FROM people")}
    turns = [dict(t) for t in repo.turns(conn, cid, "final")]
    for t in turns:
        t["speaker"] = _speaker(t, names)
    by_idx = {t["idx"]: t for t in turns}

    def can_play(idxs) -> bool:
        return bool(audio) and any((by_idx.get(i) or {}).get("t_start") is not None for i in (idxs or []))

    # Loops from this call, joined back to what the model said and what the validators noted.
    acts = actions_art.get("actions") or []
    loop_ids = reconcile.get("loop_ids") or []
    by_loop = {lid: acts[i] for i, lid in enumerate(loop_ids)} if len(loop_ids) == len(acts) else {}
    by_desc = {evidence_v.normalize(a["description"]): a for a in acts}
    loops = []
    for r in conn.execute(
            "SELECT l.*, p.name AS owner_person FROM loops l LEFT JOIN people p ON p.node_id=l.owner_person_id "
            "WHERE l.call_id=? ORDER BY CASE l.review_state WHEN 'proposed' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END, "
            f"{PRIORITY_ORDER.format(col='l.priority')}, l.created_at", (cid,)):
        loop = dict(r)
        loop["evidence"] = fromjson(loop["evidence_turns"], []) or []
        model = by_loop.get(loop["node_id"]) or by_desc.get(evidence_v.normalize(loop["description"])) or {}
        notes = model.get("validation_notes") or []
        loop["notes"] = notes if isinstance(notes, list) else [str(notes)]
        loop["model_confidence"] = model.get("confidence")
        loop["play"] = can_play(loop["evidence"])
        loops.append(loop)
    own = {l["node_id"] for l in loops}
    others = [lid for lid in loop_ids if lid not in own]
    reaffirmed = conn.execute(
        f"SELECT l.*, c.title AS call_title FROM loops l LEFT JOIN calls c ON c.node_id=l.call_id "
        f"WHERE l.node_id IN ({','.join('?' * len(others))})", others).fetchall() if others else []

    participants = [dict(p) for p in repo.call_participants(conn, cid)]
    deal = conn.execute("SELECT d.*, a.name AS account_name FROM deals d LEFT JOIN accounts a "
                        "ON a.node_id=d.account_id WHERE d.node_id=?", (call["deal_id"],)).fetchone() \
        if call["deal_id"] else None

    email = _latest_email(conn, cid)
    email_skipped = email_art.get("skipped") if isinstance(email_art, dict) else None

    clusters, seen = [], {}
    for t in turns:
        if t["channel"] != "them" or not t["speaker_cluster"]:
            continue
        entry = seen.get(t["speaker_cluster"])
        if entry is None:
            entry = seen[t["speaker_cluster"]] = {"cluster": t["speaker_cluster"], "count": 0, "person_id": t["person_id"],
                                                  "sample": t["text"][:160], "first_idx": t["idx"]}
            clusters.append(entry)
        entry["count"] += 1

    ready = state in READY_STATES
    processing = _is_processing(call)
    job = _call_job(conn, cid)
    rerun = bool(job is not None and job["type"] == "PROCESS_CALL" and ready)
    failed_step = _failed_step(call) if call["wf_error"] and state != "capture_failed" else None
    si = CHAIN.index(state) if state in CHAIN else (len(CHAIN) - 1 if ready else -1)
    steps = []
    for i, name in enumerate(CHAIN):
        if failed_step and name == failed_step:
            status = "failed"
        elif i <= si:
            status = "done"
        elif processing and i == si + 1:
            status = "current"
        else:
            status = "todo"
        steps.append({"name": name, "label": step_label(name), "status": status})

    return {
        "call": call, "cid": cid, "deal": deal, "participants": participants,
        "buyers": [p for p in participants if not p["is_me"]],
        "quality": quality, "summary": summary, "analysis": analysis, "run_ids": run_ids,
        "turns": turns, "has_audio": bool(audio), "can_play": can_play,
        "garbled_count": sum(t["quality"] == "garbled" for t in turns),
        "partial_count": sum(t["quality"] == "partial" for t in turns),
        "bleed_count": sum(bool(t["bleed_flag"]) for t in turns),
        "loops": loops, "reaffirmed": reaffirmed,
        "proposed_count": sum(l["review_state"] == "proposed" for l in loops),
        "conflicts": _open_conflicts(conn, cid, call["deal_id"]),
        "email": _email_view(conn, email) if email else None, "email_skipped": email_skipped,
        "speaker_question": _speaker_question(conn, cid) if state in workflow.HOLD_STATES else None,
        "clusters": clusters, "ready": ready, "processing": processing, "rerun": rerun, "job": job,
        "failed_step": failed_step, "steps": steps,
        "current_step": next((s for s in steps if s["status"] == "current"), None),
        "review_completed": conn.execute("SELECT 1 FROM wf_events WHERE type='REVIEW_COMPLETED' AND entity_id=?",
                                         (cid,)).fetchone() is not None,
        "deals": _deals(conn), "people": _people(conn),
        "loop_types": LOOP_TYPES, "owners": OWNERS, "priorities": PRIORITIES, "loop_statuses": LOOP_STATUSES,
        "autorefresh": 5 if (processing or rerun or state == "live") else 0,
    }


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_page(request: Request, call_id: str):
    with _db(request) as conn:
        call = _call_or_404(conn, call_id)
        return render(request, conn, "call.html", **_call_context(conn, call))


@router.post("/calls/{call_id}/retry")
def call_retry(request: Request, call_id: str, from_step: str = Form(""), next_url: str = Form("", alias="next")):
    with _db(request) as conn:
        call = _call_or_404(conn, call_id)
        if call["wf_state"] in workflow.HOLD_STATES:
            return _redirect(f"/calls/{call_id}", err="Say which speaker you are first.", anchor="speaker")
        step = from_step if from_step in workflow.STEP_NAMES else _failed_step(call)
        _publish_process(conn, call_id, step, force=False)
        repo.update_call(conn, call_id, wf_error=None, actor=ACTOR)
        conn.commit()
    return _redirect(_clean_next(next_url, f"/calls/{call_id}"), msg=f"Retry queued from {step_label(step)}.")


@router.post("/calls/{call_id}/rerun")
def call_rerun(request: Request, call_id: str, from_step: str = Form(...), force: str = Form("")):
    if from_step not in workflow.STEP_NAMES:
        return _redirect(f"/calls/{call_id}", err=f"unknown step {from_step}")
    with _db(request) as conn:
        if _call_or_404(conn, call_id)["wf_state"] in workflow.HOLD_STATES:
            return _redirect(f"/calls/{call_id}", err="Say which speaker you are first.", anchor="speaker")
        _publish_process(conn, call_id, from_step, force=force in ("1", "true", "on"))
        conn.commit()
    return _redirect(f"/calls/{call_id}", msg=f"Re-running from {step_label(from_step)}.")


@router.post("/calls/{call_id}/redraft")
def call_redraft(request: Request, call_id: str):
    with _db(request) as conn:
        _call_or_404(conn, call_id)
        _publish_process(conn, call_id, "email_drafted", force=True)
        conn.commit()
    return _redirect(f"/calls/{call_id}", msg="Redrafting the email. This page refreshes when it is ready.",
                     anchor="email")


@router.post("/calls/{call_id}/complete")
def call_complete(request: Request, call_id: str):
    with _db(request) as conn:
        call = _call_or_404(conn, call_id)
        prior = call["wf_state"]
        review.complete_review(conn, call_id)
        if prior in ("email_sent", "email_skipped", "done"):
            repo.set_call_state(conn, call_id, "done", actor=ACTOR)   # the email already closed this call
            conn.commit()
    return _redirect(f"/calls/{call_id}", msg="Review complete. Confirmed loops go to Jarvis; proposed ones stay in Open Loops.")


@router.post("/calls/{call_id}/no-email")
def call_no_email(request: Request, call_id: str):
    with _db(request) as conn:
        _call_or_404(conn, call_id)
        try:
            review.email_done(conn, call_id, sent=False)
        except ValueError as exc:
            return _redirect(f"/calls/{call_id}", err=str(exc), anchor="email")
    return _redirect(f"/calls/{call_id}", msg="Closed without a follow-up email.")


@router.get("/calls/{call_id}/clip")
def call_clip(request: Request, call_id: str, turns: str = ""):
    idxs = [int(x) for x in re.findall(r"(?<!\d)\d{1,9}(?!\d)", turns)][:50]     # absurd numbers are ignored, not chunked
    with _db(request) as conn:
        call = _call_or_404(conn, call_id)
        rows = conn.execute(
            f"SELECT t_start, t_end FROM turns WHERE call_id=? AND tier='final' AND idx IN ({','.join('?' * len(idxs))})",
            (call_id, *idxs)).fetchall() if idxs else []
    audio = _audio_files(call)
    if not audio:
        raise HTTPException(404, "no audio for this call")
    spans = [(r["t_start"], r["t_end"] if r["t_end"] is not None else r["t_start"] + 10.0)
             for r in rows if r["t_start"] is not None]
    if not spans:
        raise HTTPException(404, "these turns have no timestamps")
    start = max(0.0, min(s for s, _ in spans) - CLIP_PAD_S)
    end = min(max(e for _, e in spans) + CLIP_PAD_S, start + CLIP_MAX_S)
    try:
        wav = _mix_clip(audio, start, end)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.get("/calls/{call_id}/runs", response_class=HTMLResponse)
def call_runs(request: Request, call_id: str):
    with _db(request) as conn:
        call = _call_or_404(conn, call_id)
        runs = [_run_view(r) for r in conn.execute("SELECT * FROM agent_runs WHERE call_id=? ORDER BY id", (call_id,))]
        return render(request, conn, "runs.html", call=call, runs=runs)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int):
    with _db(request) as conn:
        run = _run_view(_one(conn, "SELECT * FROM agent_runs WHERE id=?", (run_id,), f"run {run_id}"))
        call = repo.get_call(conn, run["call_id"]) if run["call_id"] else None
        return render(request, conn, "run.html", run=run, call=call)


# ---- call: deal link, participants, speakers -----------------------------------

@router.post("/calls/{call_id}/deal")
def call_set_deal(request: Request, call_id: str, deal_id: str = Form(""), new_deal: str = Form("")):
    with _db(request) as conn:
        call = _call_or_404(conn, call_id)
        if new_deal.strip():
            deal_id = repo.create_deal(conn, new_deal.strip(), actor=ACTOR)
            repo.link_deal_person(conn, deal_id, repo.ensure_me(conn), role="seller", actor=ACTOR)
        if not deal_id:
            return _redirect(f"/calls/{call_id}", err="Pick a deal or name a new one.", anchor="link")
        _deal_or_404(conn, deal_id)
        old_deal = call["deal_id"]
        repo.update_call(conn, call_id, deal_id=deal_id, actor=ACTOR)
        for p in repo.call_participants(conn, call_id):
            if not p["is_me"]:
                repo.link_deal_person(conn, deal_id, p["node_id"], actor=ACTOR)
        # The call's own loops, emails and claims move with it (unlinked ones and ones on the old deal).
        for table in ("loops", "emails", "claims"):
            conn.execute(f"UPDATE {table} SET deal_id=? WHERE call_id=? AND (deal_id IS NULL OR deal_id IS ?)",
                         (deal_id, call_id, old_deal))
        conn.commit()
    return _redirect(f"/calls/{call_id}", msg="Linked to the deal. Redraft the email if it should use this context.",
                     anchor="link")


@router.post("/calls/{call_id}/participants")
def call_add_participant(request: Request, call_id: str, person_id: str = Form(""), name: str = Form(""),
                         email: str = Form(""), title: str = Form("")):
    with _db(request) as conn:
        call = _call_or_404(conn, call_id)
        fallback = None
        if call["deal_id"]:
            fallback = conn.execute("SELECT account_id FROM deals WHERE node_id=?", (call["deal_id"],)).fetchone()[0]
        try:
            pid = person_id or _find_or_create_person(conn, name, email, title, fallback)
        except ValueError as exc:
            return _redirect(f"/calls/{call_id}", err=str(exc), anchor="link")
        _one(conn, "SELECT 1 FROM people WHERE node_id=?", (pid,), "person")
        repo.add_participant(conn, call_id, pid)
        if call["deal_id"]:
            repo.link_deal_person(conn, call["deal_id"], pid, actor=ACTOR)
        conn.commit()
    return _redirect(f"/calls/{call_id}", msg="Participant added. If the email was skipped for lack of a recipient, press Redraft.",
                     anchor="link")


@router.post("/calls/{call_id}/speakers")
def call_map_speakers(request: Request, call_id: str, cluster: list[str] = Form(default=[]),
                      person: list[str] = Form(default=[])):
    with _db(request) as conn:
        _call_or_404(conn, call_id)
        mapping = {}
        for c, p in zip(cluster, person):
            pid = p or None
            conn.execute("UPDATE turns SET person_id=? WHERE call_id=? AND tier='final' AND channel='them' "
                         "AND speaker_cluster=?", (pid, call_id, c))
            conn.execute("INSERT INTO speakers(call_id,cluster,channel,person_id) VALUES (?,?,'them',?) "
                         "ON CONFLICT(call_id,cluster) DO UPDATE SET person_id=excluded.person_id", (call_id, c, pid))
            if pid:
                repo.add_participant(conn, call_id, pid)
            mapping[c] = pid
        stores.engine._emit(conn, ACTOR, "speakers_mapped", node_id=call_id, after=mapping)
        conn.commit()
    return _redirect(f"/calls/{call_id}", msg="Speakers mapped.", anchor="speakers")


# ---- loops --------------------------------------------------------------------

def _loop_next(loop, nxt) -> str:
    default = f"/calls/{loop['call_id']}" if loop["call_id"] else "/loops"
    return _clean_next(nxt, default)


@router.post("/loops/{loop_id}/confirm")
def loop_confirm(request: Request, loop_id: str, next_url: str = Form("", alias="next")):
    with _db(request) as conn:
        loop = _loop_or_404(conn, loop_id)
        review.confirm_loop(conn, loop_id)
        conn.commit()
    return _redirect(_loop_next(loop, next_url), msg="Confirmed.", anchor=f"loop-{loop_id}")


@router.post("/loops/{loop_id}/reject")
def loop_reject(request: Request, loop_id: str, next_url: str = Form("", alias="next")):
    with _db(request) as conn:
        loop = _loop_or_404(conn, loop_id)
        review.reject_loop(conn, loop_id)
        conn.commit()
    return _redirect(_loop_next(loop, next_url), msg="Rejected. It will not be tracked or mirrored.",
                     anchor=f"loop-{loop_id}")


@router.post("/loops/{loop_id}/status")
def loop_status(request: Request, loop_id: str, status: str = Form(...), next_url: str = Form("", alias="next")):
    with _db(request) as conn:
        loop = _loop_or_404(conn, loop_id)
        if status not in LOOP_STATUSES:
            return _redirect(_loop_next(loop, next_url), err=f"unknown status {status}")
        review.edit_loop(conn, loop_id, status=status)
        conn.commit()
    return _redirect(_loop_next(loop, next_url), msg=f"Marked {status}.", anchor=f"loop-{loop_id}")


@router.post("/loops/{loop_id}/edit")
def loop_edit(request: Request, loop_id: str, description: str = Form(""), owner: str = Form(""),
              owner_name: str = Form(""), due_date: str = Form(""), priority: str = Form(""),
              status: str = Form(""), next_url: str = Form("", alias="next")):
    with _db(request) as conn:
        loop = _loop_or_404(conn, loop_id)
        back = _loop_next(loop, next_url)
        due = due_date.strip()
        problems = []
        if not description.strip():
            problems.append("description is empty")
        if owner not in OWNERS:
            problems.append(f"owner must be one of {', '.join(OWNERS)}")
        if priority not in PRIORITIES:
            problems.append(f"priority must be one of {', '.join(PRIORITIES)}")
        if status not in LOOP_STATUSES:
            problems.append(f"status must be one of {', '.join(LOOP_STATUSES)}")
        if due:
            try:
                date.fromisoformat(due)
            except ValueError:
                problems.append("due date must be YYYY-MM-DD")
        if problems:
            return _redirect(back, err="; ".join(problems), anchor=f"loop-{loop_id}")
        wanted = {"description": description.strip(), "owner": owner, "owner_name": owner_name.strip() or None,
                  "due_date": due or None, "priority": priority, "status": status}
        changed = {k: v for k, v in wanted.items() if (loop[k] or None) != v}
        review.edit_loop(conn, loop_id, **changed)
        conn.commit()
    return _redirect(back, msg="Saved." if changed else "No changes; loop confirmed.", anchor=f"loop-{loop_id}")


@router.post("/calls/{call_id}/loops")
def call_add_loop(request: Request, call_id: str, description: str = Form(""), owner: str = Form("me"),
                  loop_type: str = Form("my_action", alias="type"), due_date: str = Form(""),
                  priority: str = Form("medium")):
    due = due_date.strip() or None
    if not description.strip() or owner not in OWNERS or loop_type not in LOOP_TYPES or priority not in PRIORITIES:
        return _redirect(f"/calls/{call_id}", err="A loop needs a description, a valid owner, type and priority.",
                         anchor="actions")
    if due:
        try:
            date.fromisoformat(due)
        except ValueError:
            return _redirect(f"/calls/{call_id}", err="due date must be YYYY-MM-DD", anchor="actions")
    with _db(request) as conn:
        _call_or_404(conn, call_id)
        loop_id = review.add_loop(conn, call_id, description.strip(), owner, loop_type, due_date=due, priority=priority)
        conn.commit()
    return _redirect(f"/calls/{call_id}", msg="Loop added and confirmed.", anchor=f"loop-{loop_id}")


@router.post("/conflicts/{conflict_id}/resolve")
def conflict_resolve(request: Request, conflict_id: int, accept: str = Form(...), next_url: str = Form("", alias="next")):
    with _db(request) as conn:
        ok = review.resolve_proposal(conn, conflict_id, accept == "1")
        conn.commit()
    back = _clean_next(next_url, "/")
    if not ok:
        return _redirect(back, err="That proposal was already settled.")
    return _redirect(back, msg="Accepted." if accept == "1" else "Rejected; the current value stays.")


@router.get("/loops", response_class=HTMLResponse)
def loops_page(request: Request):
    qp = request.query_params
    f = {"deal": qp.get("deal", ""), "owner": qp.get("owner", ""), "priority": qp.get("priority", ""),
         "status": qp.get("status", "open"), "due": qp.get("due", "any")}
    today = today_str()
    where, params = ["1=1"], []
    if f["deal"]:
        where.append("l.deal_id=?")
        params.append(f["deal"])
    if f["owner"] in OWNERS:
        where.append("l.owner=?")
        params.append(f["owner"])
    if f["priority"] in PRIORITIES:
        where.append("l.priority=?")
        params.append(f["priority"])
    if f["status"] == "open":
        where.append("l.status IN ('open','waiting')")
    elif f["status"] in LOOP_STATUSES:
        where.append("l.status=?")
        params.append(f["status"])
    else:
        f["status"] = "all"
    if f["status"] != "all":
        where.append("l.review_state!='rejected'")
    if f["due"] == "overdue":
        where.append("l.due_date IS NOT NULL AND l.due_date<?")
        params.append(today)
    elif f["due"] == "week":
        where.append("l.due_date IS NOT NULL AND l.due_date<=?")
        params.append((today_ist() + timedelta(days=6)).isoformat())
    else:
        f["due"] = "any"
    with _db(request) as conn:
        rows = conn.execute(
            "SELECT l.*, d.name AS deal_name, c.title AS call_title, c.started_at AS call_started FROM loops l "
            "LEFT JOIN deals d ON d.node_id=l.deal_id LEFT JOIN calls c ON c.node_id=l.call_id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY l.due_date IS NULL, l.due_date, {PRIORITY_ORDER.format(col='l.priority')}, l.created_at",
            params).fetchall()
        here = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return render(request, conn, "loops.html", loops=rows, f=f, deals=_deals(conn), owners=OWNERS,
                      priorities=PRIORITIES, here=_clean_next(here, "/loops"),
                      overdue=sum(1 for r in rows if r["due_date"] and r["due_date"] < today
                                  and r["status"] in ("open", "waiting")),
                      unconfirmed=sum(1 for r in rows if r["review_state"] == "proposed"))


# ---- email --------------------------------------------------------------------

def _save_form_edits(conn, row, to, cc, subject, body):
    """Store what is on screen before acting on it, so what the seller sees is what goes out."""
    if row["status"] not in ("drafted", "failed") or subject is None or body is None:
        return row
    new = (_parse_addrs(to), _parse_addrs(cc), subject.strip(), _norm_text(body))
    current = (fromjson(row["to_addrs"], []), fromjson(row["cc_addrs"], []), row["subject"] or "", row["body"] or "")
    if new != current:
        review.update_email(conn, row["id"], new[2], new[3], new[0], new[1])
        conn.commit()
        row = _email_or_404(conn, row["id"])
    return row


@router.post("/emails/{email_id}/save")
def email_save(request: Request, email_id: int, to: str = Form(""), cc: str = Form(""), subject: str = Form(""),
               body: str = Form("")):
    with _db(request) as conn:
        row = _email_or_404(conn, email_id)
        try:
            _save_form_edits(conn, row, to, cc, subject, body)
        except ValueError as exc:
            return _redirect(f"/calls/{row['call_id']}", err=str(exc), anchor="email")
        if row["status"] not in ("drafted", "failed"):
            return _redirect(f"/calls/{row['call_id']}", err="Only a draft can be edited.", anchor="email")
    return _redirect(f"/calls/{row['call_id']}", msg="Draft saved.", anchor="email")


def _approve(request: Request, email_id: int, mode: str, to, cc, subject, body):
    with _db(request) as conn:
        row = _email_or_404(conn, email_id)
        if row["kind"] != "followup" or not row["call_id"]:
            return _redirect(f"/nudges/{email_id}", err="This is a follow-up nudge; send it from its own page.")
        back = f"/calls/{row['call_id']}"
        job = _call_job(conn, row["call_id"])
        if job is not None and job["status"] == "running":
            return _redirect(back, err="A new draft is being written for this call right now. Wait for it, "
                                       "then send the version you want.", anchor="email")
        try:
            row = _save_form_edits(conn, row, to, cc, subject, body)
        except ValueError as exc:
            return _redirect(back, err=str(exc), anchor="email")
        allowed = recipients_v.allowed_recipients(conn, row["call_id"], row["deal_id"])
        addresses = (fromjson(row["to_addrs"], []) or []) + (fromjson(row["cc_addrs"], []) or [])
        # The drafter can only pick allowed addresses, so any other address here was typed by the seller.
        user_added = [a for a in addresses if a.lower() not in allowed]
        what = "Send" if mode == "send" else "Save to Gmail Drafts"
        try:
            gmail = request.app.state.gmail_factory()
            result = policy.approve_and_send(conn, email_id, gmail, mode=mode, user_added=user_added)
        except policy.SendRefused as exc:
            return _redirect(back, err=f"{what} refused: {exc}", anchor="email")
        except Exception as exc:
            return _redirect(back, err=f"{what} failed: {type(exc).__name__}: {exc}", anchor="email")
        if result.get("duplicate"):
            done = "sent" if result.get("status") == "sent" else "saved to Gmail Drafts"
            return _redirect(back, msg=f"Already {done}. Nothing was sent again.", anchor="email")
        if result.get("status") == "sent":         # what actually happened, even if this click was Save to Drafts
            try:
                review.email_done(conn, row["call_id"], sent=True)
            except ValueError as exc:
                return _redirect(back, err=str(exc), anchor="email")
            note = " The earlier attempt had already gone out; nothing was sent twice." if result.get("recovered") else ""
            return _redirect(back, msg="Sent." + note, anchor="email")
    return _redirect(back, msg="Saved to Gmail Drafts. Send it from Gmail, then mark it sent here.", anchor="email")


@router.post("/emails/{email_id}/send")
def email_send(request: Request, email_id: int, to: str = Form(None), cc: str = Form(None),
               subject: str = Form(None), body: str = Form(None)):
    return _approve(request, email_id, "send", to, cc, subject, body)


@router.post("/emails/{email_id}/draft")
def email_draft(request: Request, email_id: int, to: str = Form(None), cc: str = Form(None),
                subject: str = Form(None), body: str = Form(None)):
    return _approve(request, email_id, "draft", to, cc, subject, body)


@router.post("/emails/{email_id}/skip")
def email_skip(request: Request, email_id: int):
    with _db(request) as conn:
        row = _email_or_404(conn, email_id)
        if row["kind"] != "followup" or not row["call_id"]:
            return _redirect(f"/nudges/{email_id}", err="This is a follow-up nudge; dismiss it from its own page.")
        call_id = review.skip_email(conn, email_id)
        conn.commit()
        try:
            review.email_done(conn, call_id or row["call_id"], sent=False)
        except ValueError as exc:
            return _redirect(f"/calls/{row['call_id']}", err=str(exc), anchor="email")
    return _redirect(f"/calls/{row['call_id']}", msg="Email skipped.", anchor="email")


@router.post("/emails/{email_id}/not-sent")
def email_not_sent(request: Request, email_id: int):
    """The seller checked Gmail Sent after an unknown-delivery error: the email is not there."""
    with _db(request) as conn:
        row = _email_or_404(conn, email_id)
        if not policy.acknowledge_not_sent(conn, email_id):
            return _redirect(f"/calls/{row['call_id']}", err="Only an email stuck in sending can be released.",
                             anchor="email")
    return _redirect(f"/calls/{row['call_id']}", msg="Marked as not sent. You can send it again.", anchor="email")


@router.post("/emails/{email_id}/mark-sent")
def email_mark_sent(request: Request, email_id: int):
    with _db(request) as conn:
        row = _email_or_404(conn, email_id)
        if not policy.mark_sent_manually(conn, email_id):
            return _redirect(f"/calls/{row['call_id']}", err="Only an email saved to Gmail Drafts can be marked sent.",
                             anchor="email")
        try:
            review.email_done(conn, row["call_id"], sent=True)
        except ValueError as exc:
            return _redirect(f"/calls/{row['call_id']}", err=str(exc), anchor="email")
    return _redirect(f"/calls/{row['call_id']}", msg="Marked as sent from Gmail.", anchor="email")


# ---- deals --------------------------------------------------------------------

@router.get("/deals", response_class=HTMLResponse)
def deals_page(request: Request):
    with _db(request) as conn:
        rows = conn.execute(
            "SELECT d.*, a.name AS account_name, a.domains, "
            "(SELECT COUNT(*) FROM loops l WHERE l.deal_id=d.node_id AND l.status IN ('open','waiting') "
            " AND l.review_state!='rejected') AS open_loops, "
            "(SELECT COUNT(*) FROM loops l WHERE l.deal_id=d.node_id AND l.status IN ('open','waiting') "
            " AND l.review_state!='rejected' AND l.due_date<?) AS overdue, "
            "(SELECT COUNT(*) FROM calls c WHERE c.deal_id=d.node_id) AS n_calls, "
            "(SELECT MAX(started_at) FROM calls c WHERE c.deal_id=d.node_id) AS last_call "
            "FROM deals d LEFT JOIN accounts a ON a.node_id=d.account_id "
            "ORDER BY d.status='active' DESC, last_call DESC, d.name", (today_str(),)).fetchall()
        return render(request, conn, "deals.html", deals=rows)


@router.post("/deals")
def deal_create(request: Request, name: str = Form(""), account: str = Form(""), domains: str = Form(""),
                stage: str = Form("")):
    if not name.strip():
        return _redirect("/deals", err="A deal needs a name.")
    doms = [d.strip().lower().lstrip("@") for d in re.split(r"[,\s]+", domains) if d.strip()]
    with _db(request) as conn:
        account_id = next((a for a in (repo.find_account_by_domain(conn, d) for d in doms) if a), None)
        if account_id is None and account.strip():
            row = conn.execute("SELECT node_id FROM accounts WHERE lower(name)=lower(?)", (account.strip(),)).fetchone()
            account_id = row["node_id"] if row else None
        if account_id is None:
            account_id = repo.create_account(conn, account.strip() or name.strip(), doms, actor=ACTOR)
        deal_id = repo.create_deal(conn, name.strip(), account_id=account_id, stage=stage.strip() or None, actor=ACTOR)
        repo.link_deal_person(conn, deal_id, repo.ensure_me(conn), role="seller", actor=ACTOR)
        conn.commit()
    return _redirect(f"/deals/{deal_id}", msg="Deal created.")


@router.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(request: Request, deal_id: str):
    with _db(request) as conn:
        deal = _deal_or_404(conn, deal_id)
        calls = []
        latest_gaps, gaps_call = [], None
        for c in conn.execute("SELECT * FROM calls WHERE deal_id=? ORDER BY started_at DESC", (deal_id,)):
            analysis, _ = context.artifact(conn, c["node_id"], "analysis")
            summary, _ = context.artifact(conn, c["node_id"], "summary")
            calls.append({"call": c, "verdict": (analysis or {}).get("verdict"),
                          "next_step": ((summary or {}).get("next_step") or {}).get("text")})
            if analysis and gaps_call is None:
                latest_gaps, gaps_call = analysis.get("gaps") or [], c
        claims = {"fact": [], "inference": [], "assumption": []}
        for r in conn.execute("SELECT cl.*, c.title AS call_title FROM claims cl LEFT JOIN calls c "
                              "ON c.node_id=cl.call_id WHERE cl.deal_id=? ORDER BY cl.created_at DESC, cl.id DESC",
                              (deal_id,)):
            claim = dict(r)
            claim["evidence"] = fromjson(claim["evidence_turns"], []) or []
            claims.setdefault(claim["kind"], []).append(claim)
        subjects = {}
        for r in conn.execute("SELECT a.*, c.title AS call_title FROM assessments a LEFT JOIN calls c "
                              "ON c.node_id=a.call_id WHERE a.subject LIKE ? ORDER BY a.subject, a.created_at DESC, a.id DESC",
                              (f"deal:{deal_id}/%",)):
            short = r["subject"].split("/", 1)[-1]
            subjects.setdefault(short, {}).setdefault(r["agent"], []).append(r)
        reconciliations = {r["subject"].split("/", 1)[-1]: r for r in conn.execute(
            "SELECT * FROM reconciliations WHERE subject LIKE ? ORDER BY id", (f"deal:{deal_id}/%",))}
        loops = conn.execute(
            "SELECT l.*, c.title AS call_title FROM loops l LEFT JOIN calls c ON c.node_id=l.call_id "
            "WHERE l.deal_id=? AND l.status IN ('open','waiting') AND l.review_state!='rejected' "
            f"ORDER BY l.due_date IS NULL, l.due_date, {PRIORITY_ORDER.format(col='l.priority')}",
            (deal_id,)).fetchall()
        return render(request, conn, "deal.html", deal=deal, domains=fromjson(deal["domains"], []) or [],
                      stakeholders=repo.deal_people(conn, deal_id), loops=loops, calls=calls, claims=claims,
                      gaps=latest_gaps, gaps_call=gaps_call, subjects=subjects, reconciliations=reconciliations,
                      people=_people(conn), here=f"/deals/{deal_id}")


@router.post("/deals/{deal_id}/people")
def deal_add_person(request: Request, deal_id: str, person_id: str = Form(""), name: str = Form(""),
                    email: str = Form(""), title: str = Form(""), role: str = Form("")):
    with _db(request) as conn:
        deal = _deal_or_404(conn, deal_id)
        try:
            pid = person_id or _find_or_create_person(conn, name, email, title, deal["account_id"])
        except ValueError as exc:
            return _redirect(f"/deals/{deal_id}", err=str(exc))
        _one(conn, "SELECT 1 FROM people WHERE node_id=?", (pid,), "person")
        repo.link_deal_person(conn, deal_id, pid, role=role.strip() or None, actor=ACTOR)
        conn.commit()
    return _redirect(f"/deals/{deal_id}", msg="Stakeholder added.")


# ---- coach --------------------------------------------------------------------

@router.get("/coach", response_class=HTMLResponse)
def coach_page(request: Request):
    with _db(request) as conn:
        priority = patterns.active_priority(conn)
        rows = conn.execute(
            "SELECT * FROM seller_patterns WHERE owner_id=? AND status!='retired' ORDER BY status='active' DESC, frequency DESC, "
            "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, calls_seen DESC",
            (identity.actor_of(conn).user_id,)).fetchall()
        titles = {r["node_id"]: r for r in conn.execute("SELECT node_id, title, started_at FROM calls")}
        items = []
        for r in rows:
            p = dict(r)
            p["examples_list"] = fromjson(p["examples"], []) or []
            p["contexts_list"] = fromjson(p["contexts"], []) or []
            p["is_priority"] = priority is not None and priority["tag"] == p["tag"]
            items.append(p)
        insights, seen = [], set()
        for a in conn.execute("SELECT a.call_id, a.json, a.created_at FROM artifacts a WHERE a.kind='analysis' "
                              "ORDER BY a.id DESC"):
            if a["call_id"] in seen:
                continue
            seen.add(a["call_id"])
            data = fromjson(a["json"], {}) or {}
            if data.get("coaching_insight"):
                insights.append({"call": titles.get(a["call_id"]), "call_id": a["call_id"],
                                 "insight": data["coaching_insight"], "verdict": data.get("verdict"),
                                 "missed": data.get("biggest_missed_opportunity")})
            if len(insights) >= 5:
                break
        return render(request, conn, "coach.html", priority=next((p for p in items if p["is_priority"]), None),
                      weaknesses=[p for p in items if p["polarity"] == "weakness"],
                      strengths=[p for p in items if p["polarity"] == "strength"],
                      insights=insights, titles=titles)


# ---- import -------------------------------------------------------------------

@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    with _db(request) as conn:
        return render(request, conn, "import.html", deals=_deals(conn), people=_people(conn),
                      audio_available=_audio_importer() is not None, layouts=AUDIO_LAYOUTS)


def _started_at(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    ts = datetime.fromisoformat(raw)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=seller.zone())
    return ts.isoformat(timespec="seconds")


@router.post("/import/text")
def import_text_post(request: Request, text: str = Form(""), title: str = Form(""), deal_id: str = Form(""),
                     started_at: str = Form(""), lang_mode: str = Form("auto"),
                     participants: list[str] = Form(default=[])):
    from ..sources import paste
    if not seller.is_configured():
        return _redirect("/setup", err=NOT_CONFIGURED)
    try:
        started = _started_at(started_at)
    except ValueError:
        return _redirect("/import", err="Call date/time is not a valid date.")
    with _db(request) as conn:
        if deal_id and conn.execute("SELECT 1 FROM deals WHERE node_id=?", (deal_id,)).fetchone() is None:
            raise HTTPException(404, "deal not found")
        people = list(dict.fromkeys([repo.ensure_me(conn), *[p for p in participants if p]]))
        for person_id in people:
            if conn.execute("SELECT 1 FROM people WHERE node_id=?", (person_id,)).fetchone() is None:
                raise HTTPException(404, "person not found")
        try:
            outcome = paste.import_text(conn, _norm_text(text), title.strip() or "Imported call",
                                        deal_id=deal_id or None, started_at=started,
                                        lang_mode=lang_mode if lang_mode in LANGS else "auto", participants=people,
                                        result=True)
        except ValueError as exc:
            return _redirect("/import", err=str(exc))
    call_id = outcome.call_id
    if outcome.needs_speaker:
        return _redirect(f"/calls/{call_id}", msg="One question before this call is processed: which speaker are you?",
                         anchor="speaker")
    if not outcome.created:
        return _redirect(f"/calls/{call_id}", msg="This transcript was already imported; this is that call.")
    return _redirect(f"/calls/{call_id}", msg="Imported. Processing starts as soon as the worker is free.")


@router.post("/import/audio")
def import_audio_post(request: Request, file: UploadFile = File(...), title: str = Form(""), deal_id: str = Form(""),
                      lang_mode: str = Form("auto"), layout: str = Form("stereo_me_left"),
                      participants: list[str] = Form(default=[])):
    if not seller.is_configured():
        return _redirect("/setup", err=NOT_CONFIGURED)
    import_audio = _audio_importer()
    if import_audio is None:
        return _redirect("/import", err="Audio import is not available yet (sources/audio_file.py is missing).")
    inbox = Path(config.DATA_DIR) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    suffix = re.sub(r"[^a-z0-9.]", "", Path(file.filename or "").suffix.lower())[:10] or ".audio"
    dest = inbox / f"upload-{uuid.uuid4().hex[:12]}{suffix}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    with _db(request) as conn:
        try:
            call_id = import_audio(conn, str(dest), title.strip() or (file.filename or "Imported recording"),
                                   deal_id=deal_id or None, lang_mode=lang_mode if lang_mode in LANGS else "auto",
                                   layout=layout if layout in AUDIO_LAYOUTS else "stereo_me_left")
            for pid in dict.fromkeys([repo.ensure_me(conn), *[p for p in participants if p]]):
                repo.add_participant(conn, call_id, pid)
            conn.commit()
        except Exception as exc:
            return _redirect("/import", err=f"Audio import failed: {type(exc).__name__}: {exc}")
    return _redirect(f"/calls/{call_id}", msg="Recording imported. Transcription runs when the worker is free.")
