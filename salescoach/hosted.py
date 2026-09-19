"""Hosted mode: one instance on a container host, reached over the internet, behind one password.

Everything here is read from the environment on every call, so a test can flip it and the server
never caches a stale decision. Nothing here logs, prints or returns a password.

  SALESCOACH_PASSWORD         the password. Set = every request needs a session (web/auth.py).
  SALESCOACH_PASSWORD_HASH    the same, as pbkdf2_sha256$<iterations>$<salt>$<hex>; wins over the
                              plain one when both are set. Make one with `salescoach password-hash`.
  SALESCOACH_SESSION_SECRET   what session cookies are signed with. Unset = derived from the
                              password (hash), so changing the password logs every browser out.
  SALESCOACH_PUBLIC_URL       https://coach.example.com: the address the seller types. Set = proxy
                              mode: the platform's proxy is trusted, Origin and Host are checked
                              against this URL, and the webhook counts every request as remote.
  SALESCOACH_TRUST_PROXY      which proxy addresses uvicorn believes X-Forwarded-* from. Default
                              "*" in proxy mode (the platform owns the network), none otherwise.

Without SALESCOACH_PASSWORD (or the hash) nothing changes: the app is the localhost tool it was.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlparse

PASSWORD_ENV = "SALESCOACH_PASSWORD"
HASH_ENV = "SALESCOACH_PASSWORD_HASH"
SESSION_SECRET_ENV = "SALESCOACH_SESSION_SECRET"
PUBLIC_URL_ENV = "SALESCOACH_PUBLIC_URL"
TRUST_PROXY_ENV = "SALESCOACH_TRUST_PROXY"

HASH_SCHEME = "pbkdf2_sha256"
HASH_ITERATIONS = 600_000
COOKIE = "salescoach_session"
SESSION_DAYS = 30
SESSION_S = SESSION_DAYS * 24 * 3600
USER = "seller"                       # one seller per install; the name is in the cookie for the day there are two
LOGIN_LIMIT = 5                       # failures ...
LOGIN_WINDOW_S = 15 * 60              # ... per client address per window


# ---------------------------------------------------------------------------------- public url

def public_url():
    """The parsed SALESCOACH_PUBLIC_URL, or None when unset or not an http(s) URL with a host."""
    raw = (os.environ.get(PUBLIC_URL_ENV) or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return parsed


def proxy_mode() -> bool:
    """Is the app behind a platform proxy (a public URL is set)?"""
    return public_url() is not None


is_hosted = proxy_mode        # the wording switch in Setup: "not available in a hosted install"


def public_origin() -> Optional[str]:
    parsed = public_url()
    return f"{parsed.scheme}://{parsed.netloc.lower()}" if parsed else None


def public_host() -> Optional[tuple[str, int]]:
    """(hostname, effective port) of the public URL."""
    parsed = public_url()
    if parsed is None:
        return None
    return parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def host_matches_public(host_header: str) -> bool:
    """Does a Host header name the public URL? Hostname and port must match; a Host without a port
    means the public scheme's default port (browsers omit exactly that one)."""
    expected = public_host()
    if expected is None:
        return False
    parsed = urlparse(f"//{(host_header or '').strip()}")
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        port = 443 if public_url().scheme == "https" else 80
    return (parsed.hostname or "").lower() == expected[0] and port == expected[1]


def cookie_secure() -> bool:
    parsed = public_url()
    return bool(parsed and parsed.scheme == "https")


def trusted_proxies() -> Optional[str]:
    """uvicorn's forwarded_allow_ips: the env value, else "*" in proxy mode, else None (no proxy)."""
    explicit = (os.environ.get(TRUST_PROXY_ENV) or "").strip()
    if explicit:
        return explicit
    return "*" if proxy_mode() else None


# ------------------------------------------------------------------------------------ password

def auth_enabled() -> bool:
    return bool(os.environ.get(HASH_ENV) or os.environ.get(PASSWORD_ENV))


def make_hash(password: str, iterations: int = HASH_ITERATIONS, salt: Optional[str] = None) -> str:
    """pbkdf2_sha256$<iterations>$<salt>$<hex digest>. The salt is 16 random bytes, hex."""
    if not password:
        raise ValueError("the password is empty")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return f"{HASH_SCHEME}${int(iterations)}${salt}${digest}"


def _parse_hash(stored: str):
    parts = (stored or "").strip().split("$")
    if len(parts) != 4 or parts[0] != HASH_SCHEME:
        return None
    try:
        iterations = int(parts[1])
    except ValueError:
        return None
    if iterations < 1 or not parts[2] or not parts[3]:
        return None
    return iterations, parts[2], parts[3]


def verify_password(candidate: str) -> bool:
    """Constant-time. False when no password is configured, when the stored hash is malformed, or
    when the candidate is not a string."""
    if not isinstance(candidate, str) or not candidate:
        return False
    stored_hash = os.environ.get(HASH_ENV)
    if stored_hash:
        parsed = _parse_hash(stored_hash)
        if parsed is None:
            return False
        iterations, salt, digest = parsed
        computed = hashlib.pbkdf2_hmac("sha256", candidate.encode(), salt.encode(), iterations).hex()
        return hmac.compare_digest(computed.encode(), digest.lower().encode())
    plain = os.environ.get(PASSWORD_ENV) or ""
    if not plain:
        return False
    return hmac.compare_digest(candidate.encode(), plain.encode())


# ------------------------------------------------------------------------------------- session

def session_secret() -> bytes:
    explicit = os.environ.get(SESSION_SECRET_ENV)
    if explicit:
        return explicit.encode()
    material = os.environ.get(HASH_ENV) or os.environ.get(PASSWORD_ENV) or ""
    return hashlib.sha256(b"salescoach-session|" + material.encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(session_secret(), payload.encode(), hashlib.sha256).hexdigest()


def issue_session(now: Optional[float] = None, user: str = USER) -> str:
    """A fresh cookie value: user|issued_at|expiry|signature. Called on every login, so a login
    always rotates the cookie."""
    issued = int(now if now is not None else time.time())
    payload = f"{user}|{issued}|{issued + SESSION_S}"
    return f"{payload}|{_sign(payload)}"


def verify_session(value: Optional[str], now: Optional[float] = None) -> Optional[dict]:
    """{user, issued_at, expiry} for a valid, unexpired cookie; None for anything else."""
    if not value or not isinstance(value, str) or value.count("|") != 3:
        return None
    user, issued, expiry, signature = value.split("|")
    payload = f"{user}|{issued}|{expiry}"
    if not hmac.compare_digest(_sign(payload).encode(), signature.encode()):
        return None
    try:
        issued_at, expires_at = int(issued), int(expiry)
    except ValueError:
        return None
    moment = now if now is not None else time.time()
    if expires_at <= moment or issued_at > moment + 60 or expires_at - issued_at > SESSION_S:
        return None
    return {"user": user, "issued_at": issued_at, "expiry": expires_at}


# ---------------------------------------------------------------------------------- rate limit

class LoginLimiter:
    """LOGIN_LIMIT failures per client address per LOGIN_WINDOW_S, in memory. A success clears the
    address. One per app; a restart forgets everything, which is fine for a lockout. Bounded: past
    MAX_KEYS addresses, everything outside the window is dropped, so a flood of addresses cannot
    grow the process."""

    MAX_KEYS = 10_000

    def __init__(self, limit: int = LOGIN_LIMIT, window_s: int = LOGIN_WINDOW_S):
        self.limit, self.window_s = limit, window_s
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        if len(self._failures) <= self.MAX_KEYS:
            return
        for key in list(self._failures):
            self._recent(key, now)
        if len(self._failures) > self.MAX_KEYS:
            # Still too many live addresses: forget the ones that are not locked out, oldest first, before any lockout.
            order = sorted(self._failures, key=lambda k: (len(self._failures[k]) >= self.limit, max(self._failures[k])))
            for key in order[:len(self._failures) - self.MAX_KEYS]:
                self._failures.pop(key, None)

    def _recent(self, key: str, now: float) -> list[float]:
        kept = [t for t in self._failures.get(key, ()) if now - t < self.window_s]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)
        return kept

    def retry_after(self, key: str, now: Optional[float] = None) -> int:
        """Seconds until this address may try again; 0 when it may try now."""
        now = now if now is not None else time.time()
        with self._lock:
            recent = self._recent(key or "?", now)
            if len(recent) < self.limit:
                return 0
            return max(1, int(self.window_s - (now - min(recent))) + 1)

    def failed(self, key: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._recent(key or "?", now)
            self._failures.setdefault(key or "?", []).append(now)
            self._prune(now)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key or "?", None)
