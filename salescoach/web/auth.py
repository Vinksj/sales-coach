"""The login gate of a hosted install.

On when SALESCOACH_PASSWORD (or SALESCOACH_PASSWORD_HASH) is set; otherwise this module is inert
and the app is the localhost tool it always was. When on, every request needs a valid session
cookie except: /login, /logout, /static/*, /health, and POST /import/webhook (which has its own
shared secret and is checked by its handler).

An unauthenticated request for a page is sent to /login?next=<same-app path>; anything else (JSON,
event streams, form posts) gets a 401 so a script never follows a redirect into an HTML page.

The password is compared in constant time (hosted.verify_password), never logged, never echoed
back into the form. Failures are counted per client address (hosted.LoginLimiter): after five in
fifteen minutes the address waits. The cookie is signed (HMAC-SHA256), HttpOnly, SameSite=Lax,
Secure when the public URL is https, and rotated on every login.
"""
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import hosted

router = APIRouter()
LOGIN, LOGOUT = "/login", "/logout"
OPEN_PATHS = {LOGIN, LOGOUT, "/health"}
OPEN_PREFIXES = ("/static/",)
_NOT_A_PAGE = re.compile(r"(\.json|/events|/stream|/nudges|/clip)$")
WRONG = "That password is not right."


def _cookie(headers: dict) -> str | None:
    raw = headers.get("cookie") or ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == hosted.COOKIE:
            return value.strip()
    return None


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


class AuthGate:
    """ASGI middleware. Sits inside the same-origin guard and outside the first-run gate, so an
    unauthenticated visitor sees /login, never /setup."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and hosted.auth_enabled():
            method = scope["method"].upper()
            path = scope.get("path") or "/"
            if not is_open(method, path):
                headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
                if hosted.verify_session(_cookie(headers)) is None:
                    if _wants_page(method, path, headers):
                        query = scope.get("query_string") or b""
                        nxt = path + (("?" + query.decode("latin-1")) if query else "")
                        target = LOGIN + "?" + urlencode({"next": nxt}) if nxt != "/" else LOGIN
                        await RedirectResponse(target, status_code=303)(scope, receive, send)
                    else:
                        await JSONResponse({"error": "login required"}, status_code=401)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


# ---- pages ----------------------------------------------------------------------------------

def _context(request: Request, **ctx) -> dict:
    """base.html's chrome without touching the database: the login page must render even when
    the store is what is broken."""
    thread = getattr(request.app.state, "worker_thread", None)
    return {"path": request.url.path, "live": {"available": False, "active": False}, "nav_queued": 0, "nav_failed": 0,
            "nav_worker_on": thread is not None and thread.is_alive(), "nav_current": None,
            "flash_msg": None, "flash_err": None, "today": "", "step_names": (), **ctx}


def _render(request: Request, status: int = 200, **ctx):
    from .app import templates
    return templates.TemplateResponse(request, "login.html", _context(request, **ctx), status_code=status)


def _safe_next(raw) -> str:
    from .app import _clean_next
    nxt = _clean_next(raw, "/")
    return "/" if nxt.startswith((LOGIN, LOGOUT)) else nxt


def _client(request: Request) -> str:
    """The address the login rate limit counts. Hosted, uvicorn trusts every proxy and so takes the
    LEFTMOST X-Forwarded-For entry, which the caller can prepend; the rightmost entry is the one the
    platform's proxy appended and is what the limit keys on. Localhost: the peer itself."""
    if hosted.proxy_mode():
        chain = [part.strip() for part in (request.headers.get("x-forwarded-for") or "").split(",") if part.strip()]
        if chain:
            return chain[-1]
    return request.client.host if request.client else "?"


def _limiter(request: Request) -> hosted.LoginLimiter:
    limiter = getattr(request.app.state, "login_limiter", None)
    if limiter is None:
        limiter = request.app.state.login_limiter = hosted.LoginLimiter()
    return limiter


def _logged_in(request: Request) -> bool:
    return hosted.verify_session(request.cookies.get(hosted.COOKIE)) is not None


@router.get(LOGIN, response_class=HTMLResponse)
def login_page(request: Request):
    if not hosted.auth_enabled():
        return RedirectResponse("/", status_code=303)
    nxt = _safe_next(request.query_params.get("next"))
    if _logged_in(request):
        return RedirectResponse(nxt, status_code=303)
    return _render(request, next=nxt, error=None)


@router.post(LOGIN, response_class=HTMLResponse)
def login_post(request: Request, password: str = Form(""), next: str = Form("")):
    if not hosted.auth_enabled():
        return RedirectResponse("/", status_code=303)
    nxt = _safe_next(next)
    limiter, who = _limiter(request), _client(request)
    wait = limiter.retry_after(who)
    if wait:
        minutes = max(1, -(-wait // 60))
        return _render(request, status=429, next=nxt,
                       error=f"Too many attempts. Try again in {minutes} minute{'s' if minutes > 1 else ''}.")
    if not hosted.verify_password(password):
        limiter.failed(who)
        return _render(request, status=401, next=nxt, error=WRONG)
    limiter.succeeded(who)
    response = RedirectResponse(nxt, status_code=303)
    response.set_cookie(hosted.COOKIE, hosted.issue_session(), max_age=hosted.SESSION_S, path="/",
                        httponly=True, samesite="lax", secure=hosted.cookie_secure())
    return response


@router.post(LOGOUT)
def logout_post(request: Request):
    response = RedirectResponse(LOGIN if hosted.auth_enabled() else "/", status_code=303)
    response.delete_cookie(hosted.COOKIE, path="/")
    return response
