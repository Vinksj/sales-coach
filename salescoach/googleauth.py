"""Google as the identity provider and the OAuth grant holder of a cloud install.

Everything Google-shaped that more than one module needs: the client settings, the endpoints, the
scope sets, PKCE and the authorization URL (Authlib), the ID-token checks, the short-lived state
store between the redirect and the callback, and the one HTTP client every call to Google goes
through (mockable: tests set `transport`). No route lives here; web/auth.py owns those.

Settings, all read from the environment (or user_dir()/secrets.env through config.secret):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   the Internal OAuth client the customer's Workspace admin made
  GOOGLE_ALLOWED_DOMAINS                   comma list; the ID token's `hd` claim must be one of them
  SALESCOACH_BOOTSTRAP_ADMIN               the one address that may sign in before any invite exists

Facts this is built on (plans/sales-coach-cloud-research.md): an Internal app needs no verification
and only the org's members can sign in; `verify_oauth2_token` checks iss/aud/exp/signature but
NOT `hd` or `nonce`, so both are checked here by hand; a refresh token comes back only with
`access_type=offline` + `prompt=consent`, and `include_granted_scopes=true` keeps one grant per
user growing instead of minting a second one; `invalid_grant` on refresh means the grant is dead
(revoked, unused six months, a password change with Gmail scopes, or an admin restriction).
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from . import config, endpoints

CLIENT_ID_ENV = "GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET"
DOMAINS_ENV = "GOOGLE_ALLOWED_DOMAINS"
BOOTSTRAP_ENV = "SALESCOACH_BOOTSTRAP_ADMIN"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"          # google-auth's own default (x509)
ISSUERS = ("https://accounts.google.com", "accounts.google.com")

SIGNIN_SCOPES = ("openid", "email", "profile")
# Per feature, the least Google offers that does the job (research notes, "Scopes (minimum)"):
# gmail.compose covers save-to-Drafts AND sending, so gmail.send is not requested on top of it;
# gmail.readonly is the only scope that returns reply bodies. Never gmail.modify or mail.google.com.
FEATURE_SCOPES = {
    "gmail": ("https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"),
    "calendar": ("https://www.googleapis.com/auth/calendar.readonly",),
}
FEATURE_LABELS = {"gmail": "Gmail", "calendar": "Calendar"}

SIGNIN_CALLBACK = "/auth/callback"
CONNECT_CALLBACK = "/auth/connect/callback"
PENDING_TTL_S = 10 * 60
PENDING_MAX = 5000
HTTP_TIMEOUT_S = 15.0

# Tests replace this with an httpx.MockTransport; None = the network.
transport: Optional[httpx.BaseTransport] = None


class GoogleError(RuntimeError):
    """Google did not do what the flow needs (a failed exchange, a malformed response)."""


class Denied(PermissionError):
    """The ID token was valid but this person may not use this install (domain, verification,
    invite). The message is shown to the person; nothing in it is a secret."""


# ---- settings -----------------------------------------------------------------------------------

def client_id() -> str:
    return (config.secret(CLIENT_ID_ENV) or "").strip()


def client_secret() -> str:
    return (config.secret(CLIENT_SECRET_ENV) or "").strip()


def allowed_domains() -> list[str]:
    raw = os.environ.get(DOMAINS_ENV) or config.secret(DOMAINS_ENV) or ""
    return [d.strip().lower() for d in raw.replace(";", ",").split(",") if d.strip()]


def bootstrap_admin() -> Optional[str]:
    raw = (os.environ.get(BOOTSTRAP_ENV) or "").strip().lower()
    return raw or None


def configured() -> bool:
    """A client id, a secret and at least one allowed domain: enough to start a sign-in."""
    return bool(client_id() and client_secret() and allowed_domains())


def problems() -> list[str]:
    """What is missing for Google sign-in, in the words the deployer needs. Empty = configured."""
    out = []
    if not client_id():
        out.append(f"{CLIENT_ID_ENV} is not set")
    if not client_secret():
        out.append(f"{CLIENT_SECRET_ENV} is not set")
    if not allowed_domains():
        out.append(f"{DOMAINS_ENV} is not set (the Workspace domain(s) whose members may sign in)")
    return out


# ---- endpoints ------------------------------------------------------------------------------
# The real URLs above, unless the end-to-end harness points them at its fake (salescoach/endpoints.py:
# GOOGLE_OAUTH_BASE, honoured only with SALESCOACH_E2E=1; set without it, every call raises).

def auth_url() -> str:
    return endpoints.url(endpoints.GOOGLE_OAUTH_BASE, AUTH_URL, "/o/oauth2/v2/auth")


def token_url() -> str:
    return endpoints.url(endpoints.GOOGLE_OAUTH_BASE, TOKEN_URL, "/token")


def revoke_url() -> str:
    return endpoints.url(endpoints.GOOGLE_OAUTH_BASE, REVOKE_URL, "/revoke")


def jwks_url() -> str:
    return endpoints.url(endpoints.GOOGLE_OAUTH_BASE, JWKS_URL, "/oauth2/v3/certs")


def certs_url() -> Optional[str]:
    """google-auth's x509 certs URL when overridden; None = google-auth's own default (Google's)."""
    base = endpoints.override(endpoints.GOOGLE_OAUTH_BASE)
    return None if base is None else base + "/oauth2/v1/certs"


# ---- HTTP ---------------------------------------------------------------------------------------

def http() -> httpx.Client:
    """The client every call to Google goes through. Fixed hosts, so lib/safefetch (which exists to
    stop a response choosing the next URL) is not the tool; the transport is what tests replace."""
    return httpx.Client(transport=transport, timeout=HTTP_TIMEOUT_S, follow_redirects=False)


def exchange_code(code: str, redirect_uri: str, code_verifier: str) -> dict:
    """Authorization code -> the token response ({access_token, expires_in, id_token, refresh_token?,
    scope}). Raises GoogleError with Google's error code, never with the code or a token in it."""
    data = {"code": code, "client_id": client_id(), "client_secret": client_secret(),
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": code_verifier}
    with http() as client:
        try:
            response = client.post(token_url(), data=data)
        except httpx.HTTPError as exc:
            raise GoogleError(f"could not reach Google's token endpoint: {type(exc).__name__}") from exc
    return _token_response(response, "the code exchange")


def refresh_access_token(refresh_token: str) -> dict:
    """Refresh token -> {access_token, expires_in, scope?}. Raises GoogleError; its `code` attribute
    is Google's error string ('invalid_grant' = the grant is dead; callers mark needs_reconsent)."""
    data = {"refresh_token": refresh_token, "client_id": client_id(), "client_secret": client_secret(),
            "grant_type": "refresh_token"}
    with http() as client:
        try:
            response = client.post(token_url(), data=data)
        except httpx.HTTPError as exc:
            raise GoogleError(f"could not reach Google's token endpoint: {type(exc).__name__}") from exc
    return _token_response(response, "the refresh")


def revoke(token: str) -> bool:
    """Tell Google the grant is over. True when Google agreed; False when it did not (a token it no
    longer knows: revoked already, or dead). Never raises: the row is deleted either way."""
    with http() as client:
        try:
            response = client.post(revoke_url(), data={"token": token})
        except httpx.HTTPError:
            return False
    return response.status_code == 200


def _token_response(response: httpx.Response, what: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict) or "access_token" not in body:
        code = str(body.get("error") or f"http {response.status_code}") if isinstance(body, dict) else "malformed"
        exc = GoogleError(f"{what} failed: {code}")
        exc.code = code
        raise exc
    return body


def jwks() -> dict:
    with http() as client:
        try:
            response = client.get(jwks_url())
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GoogleError(f"could not fetch Google's signing keys: {type(exc).__name__}") from exc


# ---- the authorization request --------------------------------------------------------------

def new_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge). Authlib's RFC 7636 helper does the hashing."""
    from authlib.common.security import generate_token
    from authlib.oauth2.rfc7636 import create_s256_code_challenge
    verifier = generate_token(64)
    return verifier, create_s256_code_challenge(verifier)


def authorization_url(redirect_uri: str, scopes, state: str, nonce: str, code_challenge: str,
                      login_hint: Optional[str] = None, offline: bool = False) -> str:
    """The URL the browser is sent to. `hd` is a hint only (the callback checks the claim); the
    sign-in flow is online; a connect flow is offline + consent so a refresh token comes back, and
    include_granted_scopes keeps one grant per user (research: the 100-token limit per client)."""
    params = {"client_id": client_id(), "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(scopes), "state": state, "nonce": nonce,
              "code_challenge": code_challenge, "code_challenge_method": "S256"}
    domains = allowed_domains()
    if len(domains) == 1:
        params["hd"] = domains[0]
    elif domains:
        params["hd"] = "*"
    if login_hint:
        params["login_hint"] = login_hint
    if offline:
        params.update(access_type="offline", prompt="consent", include_granted_scopes="true")
    return auth_url() + "?" + urlencode(params)


class Pending:
    """The server side of `state`: what a redirect promised the callback (nonce, PKCE verifier,
    where to go next, which feature). One-shot, ten minutes, bounded; in memory, so one web process
    per install (Phase 6 keeps the web role to one process; a restart only means signing in again)."""

    def __init__(self, ttl_s: float = PENDING_TTL_S, max_items: int = PENDING_MAX):
        self.ttl_s, self.max_items = ttl_s, max_items
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def begin(self, **data) -> tuple[str, str, str]:
        """Remember a new attempt; returns (state, nonce, code_challenge). The verifier stays here
        until the callback takes it."""
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, challenge = new_pkce()
        now = time.time()
        with self._lock:
            self._sweep(now)
            if len(self._items) >= self.max_items:
                oldest = min(self._items, key=lambda k: self._items[k]["at"])
                self._items.pop(oldest, None)
            self._items[state] = {"nonce": nonce, "verifier": verifier, "at": now, **data}
        return state, nonce, challenge

    def take(self, state: Optional[str]) -> Optional[dict]:
        """The attempt `state` names, once; None for an unknown, expired or reused state."""
        if not state:
            return None
        with self._lock:
            self._sweep(time.time())
            return self._items.pop(state, None)

    def _sweep(self, now: float) -> None:
        dead = [k for k, v in self._items.items() if now - v["at"] > self.ttl_s]
        for k in dead:
            self._items.pop(k, None)


# ---- the ID token ---------------------------------------------------------------------------

def parse_id_token(id_token: str, nonce: str, keys: Optional[dict] = None, leeway: int = 60) -> dict:
    """Authlib: signature against Google's JWKS, iss, aud (this client), exp/iat, and the nonce we
    sent. Returns the claims. Raises GoogleError for anything that does not hold."""
    from authlib.jose import jwt
    from authlib.oidc.core import CodeIDToken
    try:
        claims = jwt.decode(id_token, keys if keys is not None else jwks(), claims_cls=CodeIDToken,
                            claims_options={"iss": {"essential": True, "values": list(ISSUERS)},
                                            "aud": {"essential": True, "values": [client_id()]}},
                            claims_params={"nonce": nonce})
        claims.validate(leeway=leeway)
    except GoogleError:
        raise
    except Exception as exc:                  # Authlib 1.8 raises joserfc errors, not only JoseError
        raise GoogleError(f"the ID token did not verify: {type(exc).__name__}: {str(exc)[:120]}") from exc
    if not claims.get("nonce") or not hmac.compare_digest(str(claims["nonce"]), nonce):
        raise GoogleError("the ID token did not verify: nonce")
    return dict(claims)


def independent_verify(id_token: str) -> dict:
    """The second opinion: google-auth's verify_oauth2_token (signature, iss, aud, exp) with its own
    key fetch. It does NOT check hd or nonce; check_claims does. Tests replace this function."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token as google_id_token
    certs = certs_url()
    if certs is None:
        return google_id_token.verify_oauth2_token(id_token, Request(), client_id())
    # The e2e harness's issuer: the same verification verify_oauth2_token does (signature, aud, exp, then
    # the issuer), against the overridden certs URL.
    info = google_id_token.verify_token(id_token, Request(), audience=client_id(), certs_url=certs)
    if info.get("iss") not in ISSUERS:
        raise GoogleError("the ID token did not verify: issuer")
    return info


def check_claims(claims: dict) -> dict:
    """What neither library checks: a verified address in an allowed Workspace domain. Returns
    {sub, email, name, hd}. Raises Denied with a message for the person."""
    email = str(claims.get("email") or "").strip().lower()
    sub = str(claims.get("sub") or "").strip()
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.lower() == "true"
    if not email or not sub:
        raise Denied("Google did not say who you are. Try again.")
    if verified is not True:
        raise Denied("Google has not verified this address. Sign in with your work Google account.")
    hd = str(claims.get("hd") or "").strip().lower()
    domains = allowed_domains()
    if not domains:
        raise Denied("This install has no allowed domain configured. Ask whoever runs it to set GOOGLE_ALLOWED_DOMAINS.")
    if not hd or hd not in domains:
        raise Denied("Sign in with your work Google account: this coach is for members of "
                     + ", ".join(domains) + ".")
    if email.rsplit("@", 1)[-1] != hd:
        raise Denied("The address on this Google account is not in its Workspace domain.")
    return {"sub": sub, "email": email, "name": str(claims.get("name") or "").strip(), "hd": hd}


def verify_id_token(id_token: str, nonce: str) -> dict:
    """Both verifications, then the claim checks: the identity a callback may act on."""
    claims = parse_id_token(id_token, nonce)
    second = independent_verify(id_token)
    if str(second.get("sub") or "") != str(claims.get("sub") or ""):
        raise GoogleError("the two ID-token verifications disagree")
    return check_claims(claims)


def granted_scopes(token_response: dict) -> list[str]:
    """The `scope` field of a token response, split. With include_granted_scopes it lists the whole grant."""
    return [s for s in str(token_response.get("scope") or "").split() if s]


def key_fingerprint(value: str) -> str:
    """For logs and audit rows: never a token, only a stable short digest of it."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]
