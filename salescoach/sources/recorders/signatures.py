"""How a push to /import/webhook/{connection_id} proves where it came from.

Two ways, per connection (sources/connections.verify_webhook decides which applies):

  standard   the recorder signs (Standard Webhooks: Granola; Fathom's HMAC scheme is taken to be the same,
             UNVERIFIED): headers webhook-id, webhook-timestamp, webhook-signature ("v1,<base64> ..."),
             signature = base64(HMAC-SHA256(key, f"{id}.{timestamp}.{body}")), key = the signing secret
             ("whsec_<base64>") the rep pasted from the recorder; the timestamp must be within 5 minutes.
  token      everything else (and a relay such as Zapier in front of any recorder): the header
             X-Salescoach-Secret carries the per-connection token the coach generated and showed once;
             only its sha256 is stored, compared in constant time.
"""
import base64
import hashlib
import hmac
import time
from typing import Optional

TOKEN_HEADER = "x-salescoach-secret"
TOLERANCE_S = 5 * 60


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def token_ok(presented: Optional[str], stored_hash: Optional[str]) -> bool:
    if not presented or not stored_hash:
        return False
    return hmac.compare_digest(token_hash(presented), str(stored_hash))


def _key(secret: str) -> bytes:
    raw = str(secret).strip()
    if raw.startswith("whsec_"):
        raw = raw[len("whsec_"):]
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return raw.encode()


def standard_sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    """The v1 signature a Standard Webhooks sender puts in webhook-signature (tests use it to sign)."""
    signed = f"{msg_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(_key(secret), signed, hashlib.sha256).digest()).decode()


def has_standard_headers(headers: dict) -> bool:
    return all(headers.get(h) for h in ("webhook-id", "webhook-timestamp", "webhook-signature"))


def standard_ok(secret: Optional[str], headers: dict, body: bytes, now: Optional[float] = None) -> bool:
    """A valid, fresh Standard Webhooks signature over exactly `body`. False without a secret."""
    if not secret or not has_standard_headers(headers):
        return False
    msg_id, stamp = headers["webhook-id"], headers["webhook-timestamp"]
    try:
        if abs((now if now is not None else time.time()) - int(stamp)) > TOLERANCE_S:
            return False
    except (TypeError, ValueError):
        return False
    expected = standard_sign(secret, msg_id, stamp, body).split(",", 1)[1]
    for candidate in str(headers["webhook-signature"]).split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected.encode()):
            return True
    return False
