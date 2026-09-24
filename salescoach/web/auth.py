"""The login gate: one password on a hosted single-seller install, Google sign-in on a cloud one.

Password mode (unchanged from the first hosted build). On when SALESCOACH_PASSWORD (or the hash)
is set and SALESCOACH_MODE is not cloud; otherwise inert. Every request needs a valid session
cookie except /login, /logout, /static/*, /health and POST /import/webhook (its own secret). The
password is compared in constant time (hosted.verify_password), never logged, never echoed back;
failures are counted per client address (hosted.LoginLimiter); the cookie is signed, HttpOnly,
SameSite=Lax, Secure over https, rotated on every login.

Cloud mode (SALESCOACH_MODE=cloud). There is no password: /login offers "Sign in with Google" and
the gate resolves every request's user from a server-side session (salescoach/sessions.py), read
together with the users row on EVERY request, so a disabled user or a revoked session is out at
the next request. The flow, and what each step refuses (plans/sales-coach-cloud-research.md):

  GET /auth/google         remembers state + nonce + a PKCE verifier server-side (googleauth.Pending)
                           and sends the browser to Google with scopes openid email profile, an
                           `hd` hint and the S256 challenge.
  GET /auth/callback       the state must be one we issued, once; the code is exchanged (with the
                           verifier); the ID token is parsed by Authlib (signature against Google's
                           JWKS, iss, aud, exp, OUR nonce) AND verified again by google-auth; then
                           email_verified, `hd` in GOOGLE_ALLOWED_DOMAINS and email in that domain
                           are checked by hand (neither library does); then the address must be an
                           invited or active user, or SALESCOACH_BOOTSTRAP_ADMIN (created once as
                           the first admin, under a lock, so two first callbacks make one row).
                           Anyone else sees "ask your admin for an invite". Failures count per
                           client address AND per address signed in (five per fifteen minutes).
  GET /auth/connect/google?feature=gmail|calendar   a signed-in user grants a feature's scopes:
                           access_type=offline, prompt=consent, include_granted_scopes=true, so a
                           refresh token comes back and ONE grant per user grows (execution/tokens).
  GET /auth/connect/callback   the consenting Google account must be the signed-in user's own.
  POST /logout             revokes this session; with everywhere=1, every session of the user.

An unauthenticated request for a page is sent to /login?next=<same-app path>; anything else (JSON,
event streams, form posts) gets a 401 so a script never follows a redirect into an HTML page.
"""
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import googleauth, hosted, identity, sessions, users
from ..store import db, stores

router = APIRouter()
LOGIN, LOGOUT = "/login", "/logout"
GOOGLE_START, GOOGLE_CALLBACK = "/auth/google", googleauth.SIGNIN_CALLBACK
CONNECT_START, CONNECT_CALLBACK = "/auth/connect/google", googleauth.CONNECT_CALLBACK
OPEN_PATHS = {LOGIN, LOGOUT, "/health", GOOGLE_START, GOOGLE_CALLBACK}
OPEN_PREFIXES = ("/static/",)
_NOT_A_PAGE = re.compile(r"(\.json|/events|/stream|/nudges|/clip)$")
WRONG = "That password is not right."
INVITE_NEEDED = ("This Google account is not on the list for this coach. Ask your admin for an invite, "
                 "then sign in again with the same account.")
DISABLED = "Your access to this coach has been switched off. Ask your admin."
EXPIRED_LINK = "That sign-in link has expired or was already used. Start again."
pending = googleauth.Pending()


def _cookie(headers: dict) -> str | None:
    raw = headers.get("cookie") or ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == hosted.COOKIE:
            return value.strip()
    return None


def required() -> bool:
    """Does every page need a signed-in person? Cloud: always. Otherwise: when a password is set."""
    return identity.cloud() or hosted.auth_enabled()


def is_open(method: str, path: str) -> bool:
    """A request that never needs a session."""
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return True
    if method == "POST":
        from ..sources.adapters import webhook
        return path == webhook.PATH
    return False


def _wants_page(method: str, path: str, headers: dict) -> bool:
    return (method in ("GET", "HEAD") and "text/html" in (headers.get("accept") or "")
            and not _NOT_A_PAGE.search(path))


async def _refuse(scope, receive, send, method, path, headers):
    if _wants_page(method, path, headers):
        query = scope.get("query_string") or b""
        nxt = path + (("?" + query.decode("latin-1")) if query else "")
        target = LOGIN + "?" + urlencode({"next": nxt}) if nxt != "/" else LOGIN
        await RedirectResponse(target, status_code=303)(scope, receive, send)
    else:
        await JSONResponse({"error": "login required"}, status_code=401)(scope, receive, send)


class AuthGate:
    """ASGI middleware. Sits inside the same-origin guard and outside the actor and first-run gates,
    so an unauthenticated visitor sees /login, never /setup. Cloud mode: resolves the session to
    its user here (one store lookup per request) and leaves the Actor in scope["state"] for the
    ActorGate to bind; open paths pass with or without one."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            method = scope["method"].upper()
            path = scope.get("path") or "/"
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if identity.cloud():
                state = scope.setdefault("state", {})
                if not path.startswith(OPEN_PREFIXES) and path != "/health":
                    resolved = _cloud_actor(scope, headers)
                    if resolved:
                        state["actor"], state["session_id"] = resolved
                if not is_open(method, path) and not state.get("actor"):
                    await _refuse(scope, receive, send, method, path, headers)
                    return
            elif hosted.auth_enabled() and not is_open(method, path):
                if hosted.verify_session(_cookie(headers)) is None:
                    await _refuse(scope, receive, send, method, path, headers)
                    return
        await self.app(scope, receive, send)


def _cloud_actor(scope, headers):
    """(Actor, session id) for a request whose cookie names a live session of an active user; None
    otherwise. Role and status come from the users row now, not from anything the cookie holds."""
    session_id = sessions.session_id_from_cookie(_cookie(headers))
    if not session_id:
        return None
    try:
        with identity.activate(None):
            conn = stores.sales(scope["app"].state.db_path)
    except Exception:
        return None
    try:
        live = sessions.resolve(conn, session_id)
        if not live:
            return None
        row = users.get(conn, live["user_id"])
        if not row or row["status"] != "active":
            return None
        return identity.Actor(row["id"], identity.INTERACTIVE, row["role"], users.profile_of(row)), session_id
    except Exception:
        return None
    finally:
        conn.close()


# ---- pages ----------------------------------------------------------------------------------

def _context(request: Request, **ctx) -> dict:
    """base.html's chrome without touching the database: the login page must render even when
    the store is what is broken."""
    thread = getattr(request.app.state, "worker_thread", None)
    return {"path": request.url.path, "live": {"available": False, "active": False}, "nav_queued": 0, "nav_failed": 0,
            "nav_worker_on": thread is not None and thread.is_alive(), "nav_current": None,
            "flash_msg": None, "flash_err": None, "today": "", "step_names": (), **ctx}


def _render(request: Request, status: int = 200, template: str = "login.html", **ctx):
    from .app import templates
    ctx.setdefault("google", identity.cloud())
    ctx.setdefault("google_ready", googleauth.configured())
    return templates.TemplateResponse(request, template, _context(request, **ctx), status_code=status)


def _denied(request: Request, message: str, status: int = 403):
    response = _render(request, status=status, template="auth_denied.html", message=message,
                       domains=googleauth.allowed_domains())
    response.headers["Cache-Control"] = "no-store"
    return response


def _safe_next(raw) -> str:
    from .app import _clean_next
    nxt = _clean_next(raw, "/")
    return "/" if nxt.startswith((LOGIN, LOGOUT, "/auth/")) else nxt


def _client(request: Request) -> str:
    """The address the login rate limit counts. Hosted, uvicorn trusts every proxy and so takes the
    LEFTMOST X-Forwarded-For entry, which the caller can prepend; the rightmost entry is the one the
    platform's proxy appended and is what the limit keys on. Localhost: the peer itself."""
    if hosted.proxy_mode():
        chain = [part.strip() for part in (request.headers.get("x-forwarded-for") or "").split(",") if part.strip()]
        if chain:
            return chain[-1]
    return request.client.host if request.client else "?"


def _limiter(request: Request, name: str = "login_limiter") -> hosted.LoginLimiter:
    limiter = getattr(request.app.state, name, None)
    if limiter is None:
        limiter = hosted.LoginLimiter()
        setattr(request.app.state, name, limiter)
    return limiter


def _wait_message(wait: int) -> str:
    minutes = max(1, -(-wait // 60))
    return f"Too many attempts. Try again in {minutes} minute{'s' if minutes > 1 else ''}."


def _logged_in(request: Request) -> bool:
    if identity.cloud():
        return getattr(request.state, "actor", None) is not None
    return hosted.verify_session(request.cookies.get(hosted.COOKIE)) is not None


def _redirect_uri(request: Request, path: str) -> str:
    base = hosted.public_origin() or str(request.base_url).rstrip("/")
    return base + path


def _db(request: Request):
    from .app import _db as app_db
    return app_db(request)


@router.get(LOGIN, response_class=HTMLResponse)
def login_page(request: Request):
    if not required():
        return RedirectResponse("/", status_code=303)
    nxt = _safe_next(request.query_params.get("next"))
    if _logged_in(request):
        return RedirectResponse(nxt, status_code=303)
    return _render(request, next=nxt, error=request.query_params.get("err") or None)


@router.post(LOGIN, response_class=HTMLResponse)
def login_post(request: Request, password: str = Form(""), next: str = Form("")):
    if identity.cloud():
        return RedirectResponse(LOGIN, status_code=303)         # there is no password to post
    if not hosted.auth_enabled():
        return RedirectResponse("/", status_code=303)
    nxt = _safe_next(next)
    limiter, who = _limiter(request), _client(request)
    wait = limiter.retry_after(who)
    if wait:
        return _render(request, status=429, next=nxt, error=_wait_message(wait))
    if not hosted.verify_password(password):
        limiter.failed(who)
        return _render(request, status=401, next=nxt, error=WRONG)
    limiter.succeeded(who)
    response = RedirectResponse(nxt, status_code=303)
    response.set_cookie(hosted.COOKIE, hosted.issue_session(), max_age=hosted.SESSION_S, path="/",
                        httponly=True, samesite="lax", secure=hosted.cookie_secure())
    return response


@router.post(LOGOUT)
def logout_post(request: Request, everywhere: str = Form("")):
    if identity.cloud():
        actor = getattr(request.state, "actor", None)
        session_id = getattr(request.state, "session_id", None)
        if actor is not None:
            with _db(request) as conn:
                if everywhere:
                    sessions.revoke_all(conn, actor.user_id)
                else:
                    sessions.revoke(conn, session_id)
        response = RedirectResponse(LOGIN, status_code=303)
        sessions.clear_cookie(response)
        return response
    response = RedirectResponse(LOGIN if hosted.auth_enabled() else "/", status_code=303)
    response.delete_cookie(hosted.COOKIE, path="/")
    return response


# ---- Google sign-in ---------------------------------------------------------------------------

def _not_cloud(request: Request):
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Not found")


@router.get(GOOGLE_START)
def google_start(request: Request):
    if not identity.cloud():
        _not_cloud(request)
    nxt = _safe_next(request.query_params.get("next"))
    if _logged_in(request):
        return RedirectResponse(nxt, status_code=303)
    if not googleauth.configured():
        return _render(request, status=503, next=nxt,
                       error="Google sign-in is not configured on this install: " + "; ".join(googleauth.problems()) + ".")
    wait = _limiter(request).retry_after(_client(request))
    if wait:
        return _render(request, status=429, next=nxt, error=_wait_message(wait))
    state, nonce, challenge = pending.begin(kind="signin", next=nxt)
    url = googleauth.authorization_url(_redirect_uri(request, GOOGLE_CALLBACK), googleauth.SIGNIN_SCOPES, state, nonce,
                                       challenge)
    response = RedirectResponse(url, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def _bootstrap_admin(conn, ident: dict) -> dict:
    """The first admin, exactly once: a lock around check-then-insert, and the UNIQUE address as the
    backstop, so two first callbacks racing end with one row and both signed in as it."""
    conn.serialize("bootstrap-admin")
    row = users.by_email(conn, ident["email"])
    if row is not None:
        conn.commit()
        return row
    try:
        row = users.create(conn, ident["email"], ident["name"], role="admin", status="active", google_sub=ident["sub"])
        with identity.as_actor(conn, users.as_actor(row)):
            users.audit(conn, "user.bootstrap", {"email": ident["email"], "role": "admin"})
        conn.commit()
    except db.IntegrityError:
        conn.rollback()
        row = users.by_email(conn, ident["email"])
    return row


def _resolve_user(conn, ident: dict) -> dict:
    """The users row a verified Google identity may act as. Raises googleauth.Denied otherwise."""
    from ..store.stores import now
    row = users.by_email(conn, ident["email"])
    if row is None and googleauth.bootstrap_admin() == ident["email"]:
        row = _bootstrap_admin(conn, ident)
    if row is None:
        raise googleauth.Denied(INVITE_NEEDED)
    if row["status"] == "disabled":
        raise googleauth.Denied(DISABLED)
    if row["google_sub"] and row["google_sub"] != ident["sub"]:
        raise googleauth.Denied("This address is linked to a different Google account. Ask your admin.")
    fields = {}
    if not row["google_sub"]:
        fields["google_sub"] = ident["sub"]
    if not row["name"] and ident["name"]:
        fields["name"] = ident["name"]
    if row["status"] == "invited":
        fields["status"] = "active"
        conn.execute("UPDATE invites SET accepted_at=? WHERE email=? AND accepted_at IS NULL", (now(), ident["email"]))
    if fields:
        row = users.update(conn, row["id"], **fields)
    conn.commit()
    return row


@router.get(GOOGLE_CALLBACK)
def google_callback(request: Request):
    if not identity.cloud():
        _not_cloud(request)
    ip_limiter, who = _limiter(request), _client(request)
    wait = ip_limiter.retry_after(who)
    if wait:
        return _render(request, status=429, next="/", error=_wait_message(wait))
    params = request.query_params
    data = pending.take(params.get("state"))
    if not data or data.get("kind") != "signin":
        ip_limiter.failed(who)
        return _render(request, status=400, next="/", error=EXPIRED_LINK)
    nxt = data.get("next") or "/"
    if params.get("error") or not params.get("code"):
        return _render(request, status=400, next=nxt,
                       error="Google did not complete the sign-in (" + (params.get("error") or "no code") + "). Try again.")
    try:
        token = googleauth.exchange_code(params["code"], _redirect_uri(request, GOOGLE_CALLBACK), data["verifier"])
        ident = googleauth.verify_id_token(str(token.get("id_token") or ""), data["nonce"])
    except googleauth.Denied as exc:
        ip_limiter.failed(who)
        return _denied(request, str(exc))
    except googleauth.GoogleError as exc:
        ip_limiter.failed(who)
        return _render(request, status=502, next=nxt, error=f"Sign-in did not complete: {exc}. Try again.")
    email_limiter = _limiter(request, "email_limiter")
    wait = email_limiter.retry_after(ident["email"])
    if wait:
        return _render(request, status=429, next=nxt, error=_wait_message(wait))
    with _db(request) as conn:
        try:
            row = _resolve_user(conn, ident)
        except googleauth.Denied as exc:
            conn.rollback()
            ip_limiter.failed(who)
            email_limiter.failed(ident["email"])
            return _denied(request, str(exc))
        with identity.as_actor(conn, users.as_actor(row)):        # the session and audit rows are this user's
            _session_id, cookie = sessions.create(conn, row["id"], ip=who, user_agent=request.headers.get("user-agent"))
            users.audit(conn, "user.signin", {"email": row["email"]})
            conn.commit()
    ip_limiter.succeeded(who)
    email_limiter.succeeded(ident["email"])
    response = RedirectResponse(nxt, status_code=303)
    sessions.set_cookie(response, cookie)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---- incremental consent: Gmail, Calendar ---------------------------------------------------

ME_SETUP = "/me/setup"


def _me_redirect(msg=None, err=None):
    from .app import _redirect
    return _redirect(ME_SETUP, msg=msg, err=err)


@router.get(CONNECT_START)
def connect_start(request: Request):
    from ..execution import tokens
    if not identity.cloud():
        _not_cloud(request)
    actor = identity.current_actor()
    feature = (request.query_params.get("feature") or "").strip().lower()
    if feature not in googleauth.FEATURE_SCOPES:
        return _me_redirect(err="Choose what to connect: Gmail or Calendar.")
    if not googleauth.configured():
        return _me_redirect(err="Google is not configured on this install: " + "; ".join(googleauth.problems()) + ".")
    if not tokens.keys_configured():
        return _me_redirect(err="This install cannot hold a Google connection yet: SALESCOACH_TOKEN_KEYS is not set. "
                                "Ask whoever runs it.")
    state, nonce, challenge = pending.begin(kind="connect", user_id=actor.user_id, feature=feature)
    email = (actor.profile or {}).get("emails", [None])[0] if actor.profile else None
    scopes = (*googleauth.SIGNIN_SCOPES, *googleauth.FEATURE_SCOPES[feature])
    url = googleauth.authorization_url(_redirect_uri(request, CONNECT_CALLBACK), scopes, state, nonce, challenge,
                                       login_hint=email, offline=True)
    response = RedirectResponse(url, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(CONNECT_CALLBACK)
def connect_callback(request: Request):
    from ..execution import tokens
    if not identity.cloud():
        _not_cloud(request)
    actor = identity.current_actor()
    params = request.query_params
    data = pending.take(params.get("state"))
    if not data or data.get("kind") != "connect" or data.get("user_id") != actor.user_id:
        return _me_redirect(err="That connection link has expired or was already used. Start again.")
    feature = data["feature"]
    label = googleauth.FEATURE_LABELS[feature]
    if params.get("error") or not params.get("code"):
        return _me_redirect(err=f"Google did not complete the {label} connection ({params.get('error') or 'no code'}).")
    try:
        token = googleauth.exchange_code(params["code"], _redirect_uri(request, CONNECT_CALLBACK), data["verifier"])
        ident = googleauth.verify_id_token(str(token.get("id_token") or ""), data["nonce"])
    except googleauth.Denied as exc:
        return _me_redirect(err=f"{label} was not connected: {exc}")
    except googleauth.GoogleError as exc:
        return _me_redirect(err=f"{label} was not connected: {exc}.")
    with _db(request) as conn:
        me = users.get(conn, actor.user_id)
        if not me or not me["email"] or ident["email"] != me["email"]:
            return _me_redirect(err=f"{label} was not connected: consent with your own account ({(me or {}).get('email')}), "
                                    f"not {ident['email']}.")
        granted = googleauth.granted_scopes(token) or list(googleauth.FEATURE_SCOPES[feature])
        missing = [s for s in googleauth.FEATURE_SCOPES[feature] if s not in granted]
        if missing:
            return _me_redirect(err=f"{label} was not connected: Google did not grant everything it needs. "
                                    "Tick every box on the consent screen and try again.")
        try:
            tokens.store(conn, actor.user_id, token.get("refresh_token"), granted, ident["email"],
                         access_token=token.get("access_token"), expires_in=token.get("expires_in"))
        except tokens.TokenError as exc:
            conn.rollback()
            return _me_redirect(err=f"{label} was not connected: {exc}")
        users.audit(conn, "google.connect", {"feature": feature, "email": ident["email"],
                                             "scopes": sorted(googleauth.FEATURE_SCOPES[feature])})
        conn.commit()
    return _me_redirect(msg=f"{label} connected as {ident['email']}.")
