"""Where the app's fixed third-party endpoints live, and the one way to point them elsewhere.

Every Google and recorder URL the app calls is a constant in the module that calls it (googleauth,
execution/gmail, automation/gcal, sources/recorders/*). The end-to-end harness (e2e/, docs/deploy-cloud.md
"End-to-end check") runs the whole stack in Docker against fakes of those services, so each URL can be
overridden by an environment variable, and ONLY for that harness:

  GOOGLE_OAUTH_BASE         accounts.google.com + oauth2.googleapis.com + the signing keys: the authorization
                            page (<base>/o/oauth2/v2/auth), token (<base>/token), revoke (<base>/revoke),
                            JWKS (<base>/oauth2/v3/certs) and the x509 certs (<base>/oauth2/v1/certs)
  GOOGLE_API_BASE           the Gmail API root (<base>/gmail/v1/...) and Calendar (<base>/calendar/v3/...)
  SALESCOACH_FIREFLIES_URL  the Fireflies GraphQL endpoint
  SALESCOACH_FATHOM_BASE    the Fathom REST base (what https://api.fathom.ai/external/v1 is)

Unset (every real install) means the real URL, unchanged. Set, it is honoured only when SALESCOACH_E2E=1 is
also set; otherwise every call that would use it raises OverrideRefused and `salescoach serve` refuses to
start (problems()), so a stray variable in a production environment cannot send Google codes, tokens or a
rep's recorder key anywhere but the vendor. Nothing about what is checked changes: an ID token from an
overridden issuer is still verified (signature against the keys at the overridden URL, iss, aud, exp, nonce,
hd), exactly as Google's are.
"""
import os
from typing import Optional

E2E_ENV = "SALESCOACH_E2E"
GOOGLE_OAUTH_BASE = "GOOGLE_OAUTH_BASE"
GOOGLE_API_BASE = "GOOGLE_API_BASE"
FIREFLIES_URL = "SALESCOACH_FIREFLIES_URL"
FATHOM_BASE = "SALESCOACH_FATHOM_BASE"
ALL = (GOOGLE_OAUTH_BASE, GOOGLE_API_BASE, FIREFLIES_URL, FATHOM_BASE)


class OverrideRefused(RuntimeError):
    """An endpoint override is set outside the end-to-end harness."""


def e2e() -> bool:
    return (os.environ.get(E2E_ENV) or "").strip() == "1"


def override(env: str) -> Optional[str]:
    """The override in `env` (no trailing slash), None when unset. Raises OverrideRefused when it is set
    without SALESCOACH_E2E=1."""
    raw = (os.environ.get(env) or "").strip()
    if not raw:
        return None
    if not e2e():
        raise OverrideRefused(f"{env} is set but {E2E_ENV} is not 1: endpoint overrides exist only for the "
                              "end-to-end harness (e2e/); unset it")
    return raw.rstrip("/")


def url(env: str, real: str, path: str = "") -> str:
    """`real` unless `env` overrides it; with an override, <override><path>."""
    base = override(env)
    return real if base is None else base + path


def problems() -> list[str]:
    """Overrides set without SALESCOACH_E2E=1, in the words the deployer needs. Empty = fine."""
    if e2e():
        return []
    return [f"{env} is set (an end-to-end test override) but {E2E_ENV} is not 1: unset it"
            for env in ALL if (os.environ.get(env) or "").strip()]
