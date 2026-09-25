"""Encrypted OAuth grants: one live row per (user, provider) in `oauth_tokens`.

Google requires tokens encrypted at rest; this is AES-256-GCM under a key ring the process reads
from the environment and never writes anywhere:

  SALESCOACH_TOKEN_KEYS = "kid:<base64 32 bytes>,kid2:<base64 32 bytes>,..."

The FIRST key encrypts; any listed key decrypts (its kid is stored beside the ciphertext). To
rotate: add the new key at the front, deploy, run `salescoach tokens rotate` (re-encrypts every
row under the new key), then drop the old key. Each ciphertext is bound to its row (user, provider,
column) as GCM associated data, so a value moved between rows fails to decrypt. Nothing here logs,
prints, renders or returns a token except to the code that must hand it to Google.

Row status:  active           usable
             needs_reconsent  Google answered invalid_grant to a refresh: the person re-links
                              from /me/setup (research: revoked, unused six months, a password
                              change with Gmail scopes, an admin restriction). Never retried in a loop.
             revoked          an admin disabled the user; the row stays so the page can say so
"""
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import googleauth
from ..store.stores import now

KEYS_ENV = "SALESCOACH_TOKEN_KEYS"
PROVIDER = "google"
STATUSES = ("active", "needs_reconsent", "revoked")
ACCESS_MARGIN_S = 120          # a cached access token this close to expiry is refreshed first
NONCE_BYTES = 12


class TokenError(RuntimeError):
    pass


class NoKeys(TokenError):
    """SALESCOACH_TOKEN_KEYS is missing or malformed: the install cannot hold a grant."""


class NoToken(TokenError):
    """This user has no live grant for the provider (never connected, or disconnected)."""


class NeedsReconsent(TokenError):
    """The grant is dead at Google's end; the person must connect again."""


# ---- the key ring ------------------------------------------------------------------------------

def key_ring() -> list[tuple[str, bytes]]:
    """[(kid, key), ...] newest first. Raises NoKeys when unset or any entry is not kid:base64(32 bytes)."""
    raw = (os.environ.get(KEYS_ENV) or "").strip()
    if not raw:
        raise NoKeys(f"{KEYS_ENV} is not set: make one with `salescoach tokens new-key`")
    ring, seen = [], set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        kid, sep, material = entry.partition(":")
        kid = kid.strip()
        if not sep or not kid or ":" in material or "," in kid or "." in kid:
            raise NoKeys(f"{KEYS_ENV}: each entry is kid:base64key")
        try:
            key = base64.b64decode(material.strip(), validate=True)
        except (ValueError, TypeError):
            raise NoKeys(f"{KEYS_ENV}: key {kid!r} is not base64") from None
        if len(key) != 32:
            raise NoKeys(f"{KEYS_ENV}: key {kid!r} must be 32 bytes (256 bits) before base64")
        if kid in seen:
            raise NoKeys(f"{KEYS_ENV}: kid {kid!r} appears twice")
        seen.add(kid)
        ring.append((kid, key))
    if not ring:
        raise NoKeys(f"{KEYS_ENV} is empty")
    return ring


def new_key_line(kid: Optional[str] = None) -> str:
    """A fresh kid:base64 entry for SALESCOACH_TOKEN_KEYS (the CLI prints it; nothing stores it)."""
    kid = kid or datetime.now(timezone.utc).strftime("k%Y%m%d%H%M%S")
    return f"{kid}:{base64.b64encode(os.urandom(32)).decode()}"


def keys_configured() -> bool:
    try:
        key_ring()
        return True
    except NoKeys:
        return False


def _key(kid: str) -> bytes:
    for k, key in key_ring():
        if k == kid:
            return key
    raise TokenError(f"no key {kid!r} in {KEYS_ENV}: a rotated-out key is still needed to read this row")


def _aad(user_id: str, provider: str, column: str) -> bytes:
    return f"{user_id}|{provider}|{column}".encode()


def encrypt(plaintext: str, aad: bytes) -> tuple[str, str]:
    """(ciphertext as base64 of nonce||ct||tag, kid of the key used = the newest)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    kid, key = key_ring()[0]
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    return base64.b64encode(nonce + ct).decode(), kid


def decrypt(ciphertext: str, kid: str, aad: bytes) -> str:
    """Raises TokenError for a tampered ciphertext, a wrong row (aad) or a key not in the ring."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _key(kid)
    try:
        blob = base64.b64decode(ciphertext, validate=True)
        if len(blob) <= NONCE_BYTES:
            raise ValueError("short")
        return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad).decode()
    except (InvalidTag, ValueError, TypeError) as exc:
        raise TokenError("the stored token could not be decrypted (tampered, or the wrong key)") from exc


# ---- rows ----------------------------------------------------------------------------------

def _row(r) -> Optional[dict]:
    if r is None:
        return None
    out = dict(r)
    try:
        out["scopes"] = json.loads(out.get("scopes") or "[]")
    except ValueError:
        out["scopes"] = []
    return out


def get(conn, user_id: str, provider: str = PROVIDER) -> Optional[dict]:
    """The row with its ciphertexts (never decrypted here); None when the user has none."""
    return _row(conn.execute("SELECT * FROM oauth_tokens WHERE user_id=? AND provider=?", (user_id, provider)).fetchone())


def status_of(conn, user_id: str, provider: str = PROVIDER) -> dict:
    """{status: not_connected|active|needs_reconsent|revoked, email, scopes, features, last_error}
    for the /me/setup cards. Feature = every scope of FEATURE_SCOPES[feature] is in the grant."""
    row = get(conn, user_id, provider)
    if row is None:
        return {"status": "not_connected", "email": None, "scopes": [], "features": [], "last_error": None}
    have = set(row["scopes"])
    features = [f for f, scopes in googleauth.FEATURE_SCOPES.items() if set(scopes) <= have]
    return {"status": row["status"], "email": row["email"], "scopes": sorted(have), "features": features,
            "last_error": row["last_error"]}


def has_feature(conn, user_id: str, feature: str, provider: str = PROVIDER) -> bool:
    row = get(conn, user_id, provider)
    return bool(row and row["status"] == "active" and set(googleauth.FEATURE_SCOPES[feature]) <= set(row["scopes"]))


def store(conn, user_id: str, refresh_token: Optional[str], scopes, email: Optional[str],
          access_token: Optional[str] = None, expires_in: Optional[float] = None, provider: str = PROVIDER) -> dict:
    """Upsert the one row: a new refresh token replaces the old one (Google minted it for the same
    client; the old one is superseded), scopes are the union of what was granted before and now,
    status goes back to active. refresh_token=None keeps the stored one (a re-consent without a new
    token is rare but Google documents it)."""
    existing = get(conn, user_id, provider)
    merged = sorted(set(existing["scopes"] if existing else []) | set(scopes or ()))
    rt_enc = kid = None
    if refresh_token:
        rt_enc, kid = encrypt(refresh_token, _aad(user_id, provider, "refresh_token"))
    elif existing and existing["refresh_token_enc"]:
        rt_enc, kid = existing["refresh_token_enc"], existing["key_id"]
    else:
        raise TokenError("Google returned no refresh token. Remove the coach under your Google account's "
                         "third-party access, then connect again.")
    at_enc, expires_at = None, None
    if access_token:
        at_enc, at_kid = encrypt(access_token, _aad(user_id, provider, "access_token"))
        if at_kid != kid:                      # one key per row: re-encrypt the refresh token under the newest
            rt_enc, kid = encrypt(decrypt(rt_enc, kid, _aad(user_id, provider, "refresh_token")),
                                  _aad(user_id, provider, "refresh_token"))
        expires_at = _expiry(expires_in)
    stamp = now()
    if existing:
        conn.execute("UPDATE oauth_tokens SET scopes=?, refresh_token_enc=?, access_token_enc=?, key_id=?, expires_at=?, "
                     "email=?, status='active', last_error=NULL, updated_at=? WHERE user_id=? AND provider=?",
                     (json.dumps(merged), rt_enc, at_enc, kid, expires_at, email or existing["email"], stamp,
                      user_id, provider))
    else:
        conn.execute("INSERT INTO oauth_tokens(user_id,provider,scopes,refresh_token_enc,access_token_enc,key_id,"
                     "expires_at,email,status,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'active',NULL,?,?)",
                     (user_id, provider, json.dumps(merged), rt_enc, at_enc, kid, expires_at, email, stamp, stamp))
    return get(conn, user_id, provider)


def mark(conn, user_id: str, status: str, error: Optional[str] = None, provider: str = PROVIDER) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    conn.execute("UPDATE oauth_tokens SET status=?, last_error=?, access_token_enc=NULL, expires_at=NULL, updated_at=? "
                 "WHERE user_id=? AND provider=?", (status, (error or None) and str(error)[:500], now(), user_id, provider))


def delete(conn, user_id: str, provider: str = PROVIDER) -> bool:
    cur = conn.execute("DELETE FROM oauth_tokens WHERE user_id=? AND provider=?", (user_id, provider))
    return bool(cur.rowcount)


def _expiry(expires_in) -> Optional[str]:
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _fresh(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False
    try:
        until = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until - datetime.now(timezone.utc) > timedelta(seconds=ACCESS_MARGIN_S)


# ---- using a grant --------------------------------------------------------------------------

def refresh_token_of(conn, user_id: str, provider: str = PROVIDER) -> str:
    """The plaintext refresh token of a live grant, for the code that must hand it to Google.
    NoToken when there is no row; NeedsReconsent when the row is not active."""
    row = get(conn, user_id, provider)
    if row is None or not row["refresh_token_enc"]:
        raise NoToken(f"{googleauth.FEATURE_LABELS.get(provider, provider).title()} is not connected for this user")
    if row["status"] != "active":
        raise NeedsReconsent("the Google connection needs to be linked again" +
                             (f" ({row['last_error']})" if row["last_error"] else ""))
    return decrypt(row["refresh_token_enc"], row["key_id"], _aad(user_id, provider, "refresh_token"))


def access_token(conn, user_id: str, provider: str = PROVIDER) -> str:
    """A usable access token: the cached one while it is fresh, else one refresh (Google, through
    googleauth.refresh_access_token), cached back encrypted. invalid_grant marks the row
    needs_reconsent and raises NeedsReconsent; nothing here retries."""
    row = get(conn, user_id, provider)
    if row is None or not row["refresh_token_enc"]:
        raise NoToken("Google is not connected for this user")
    if row["status"] != "active":
        raise NeedsReconsent("the Google connection needs to be linked again" +
                             (f" ({row['last_error']})" if row["last_error"] else ""))
    if row["access_token_enc"] and _fresh(row["expires_at"]):
        try:
            return decrypt(row["access_token_enc"], row["key_id"], _aad(user_id, provider, "access_token"))
        except TokenError:
            pass                                          # a stale cache under a dropped key: refresh instead
    refresh = decrypt(row["refresh_token_enc"], row["key_id"], _aad(user_id, provider, "refresh_token"))
    try:
        answer = googleauth.refresh_access_token(refresh)
    except googleauth.GoogleError as exc:
        code = getattr(exc, "code", "")
        if code == "invalid_grant":
            mark(conn, user_id, "needs_reconsent", "Google no longer accepts the link (invalid_grant)", provider)
            conn.commit()
            raise NeedsReconsent("Google no longer accepts the link; connect Google again from your profile page") from exc
        conn.execute("UPDATE oauth_tokens SET last_error=?, updated_at=? WHERE user_id=? AND provider=?",
                     (str(exc)[:500], now(), user_id, provider))
        conn.commit()
        raise TokenError(str(exc)) from exc
    token = str(answer["access_token"])
    at_enc, kid = encrypt(token, _aad(user_id, provider, "access_token"))
    if kid != row["key_id"]:
        rt_enc, kid = encrypt(refresh, _aad(user_id, provider, "refresh_token"))
        conn.execute("UPDATE oauth_tokens SET refresh_token_enc=?, key_id=? WHERE user_id=? AND provider=?",
                     (rt_enc, kid, user_id, provider))
    conn.execute("UPDATE oauth_tokens SET access_token_enc=?, expires_at=?, last_error=NULL, updated_at=? "
                 "WHERE user_id=? AND provider=?", (at_enc, _expiry(answer.get("expires_in")), now(), user_id, provider))
    conn.commit()
    return token


def disconnect(conn, user_id: str, provider: str = PROVIDER) -> dict:
    """The person's own Disconnect: revoke at Google (best effort), delete the row.
    Returns {"had": bool, "revoked": bool}."""
    row = get(conn, user_id, provider)
    if row is None:
        return {"had": False, "revoked": False}
    revoked = False
    if row["refresh_token_enc"]:
        try:
            revoked = googleauth.revoke(decrypt(row["refresh_token_enc"], row["key_id"],
                                                _aad(user_id, provider, "refresh_token")))
        except TokenError:
            revoked = False
    delete(conn, user_id, provider)
    return {"had": True, "revoked": revoked}


def revoke_all_for_user(conn, user_id: str) -> int:
    """An admin disabled the user: every grant is revoked at Google (best effort) and marked revoked
    here, so nothing can act as that person again. Returns how many rows were touched."""
    rows = conn.execute("SELECT * FROM oauth_tokens WHERE user_id=?", (user_id,)).fetchall()
    for r in rows:
        if r["refresh_token_enc"] and r["status"] != "revoked":
            try:
                googleauth.revoke(decrypt(r["refresh_token_enc"], r["key_id"], _aad(user_id, r["provider"], "refresh_token")))
            except TokenError:
                pass
        mark(conn, user_id, "revoked", "the user was disabled by an admin", r["provider"])
    return len(rows)


# ---- rotation ---------------------------------------------------------------------------------

def rotate(conn) -> dict:
    """Re-encrypt every row under the newest key. Rows already on it are skipped; a row whose key is
    no longer in the ring is reported, not touched. Returns {rotated, skipped, unreadable}."""
    newest = key_ring()[0][0]
    rotated = skipped = 0
    unreadable = []
    for r in conn.execute("SELECT * FROM oauth_tokens ORDER BY user_id, provider").fetchall():
        if r["key_id"] == newest:
            skipped += 1
            continue
        try:
            rt = decrypt(r["refresh_token_enc"], r["key_id"], _aad(r["user_id"], r["provider"], "refresh_token")) \
                if r["refresh_token_enc"] else None
            at = decrypt(r["access_token_enc"], r["key_id"], _aad(r["user_id"], r["provider"], "access_token")) \
                if r["access_token_enc"] else None
        except TokenError:
            unreadable.append(f"{r['user_id']}/{r['provider']} (key {r['key_id']})")
            continue
        rt_enc = encrypt(rt, _aad(r["user_id"], r["provider"], "refresh_token"))[0] if rt else None
        at_enc = encrypt(at, _aad(r["user_id"], r["provider"], "access_token"))[0] if at else None
        conn.execute("UPDATE oauth_tokens SET refresh_token_enc=?, access_token_enc=?, key_id=?, updated_at=? "
                     "WHERE user_id=? AND provider=?", (rt_enc, at_enc, newest, now(), r["user_id"], r["provider"]))
        rotated += 1
    conn.commit()
    return {"rotated": rotated, "skipped": skipped, "unreadable": unreadable, "key": newest}
