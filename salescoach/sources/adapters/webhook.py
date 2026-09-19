"""Webhook: POST /import/webhook with the generic JSON and the shared secret.

This is how a recorder with an automation (Zapier, Make, n8n, a recorder's own "send to webhook")
reaches the coach. The route lives in sources/web.py; the narrow exemption from the browser
same-origin guard lives in web/app.py and both call `authorised()` below.

The secret is config.secret("WEBHOOK_SECRET"). No secret set means the webhook is OFF: there is no
default and no unauthenticated mode.

LOCAL ONLY BY DEFAULT (review 3). With the secret alone the exemption used to apply to a request from
anywhere, which is what a tunnel needs and also what makes a badly configured tunnel dangerous. Now
the option `allow_remote` of the webhook source (sources.yaml, the switch in Setup) decides:
  off (the default)  only a request that is provably from this machine is let through: a loopback
                     client address, a local Host header and no proxy/tunnel forwarding header;
  on                 any request with the right secret, as before. The Setup page says what the tunnel
                     must then do: forward ONLY /import/webhook, and never rewrite the Host header.

HOSTED (SALESCOACH_PUBLIC_URL set): every request arrives through the platform's proxy, so none is
"from this machine"; is_local() is False and the webhook works only with allow_remote on. The
secret check is the same either way.
"""
import hmac
import ipaddress
from typing import Optional

from ... import config, hosted
from .. import base, parsers
from . import MAX_BYTES, Adapter, SourceError

SECRET_NAME = "WEBHOOK_SECRET"
HEADER = "x-salescoach-secret"
PATH = "/import/webhook"
REMOTE_REFUSED = ("the webhook only answers requests from this machine; switch on 'Allow requests through a tunnel' "
                  "in Setup to change that")
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})     # testserver: FastAPI's TestClient only
LOCAL_CLIENTS = frozenset({"localhost", "testclient"})                       # testclient: the same
# What a reverse proxy or a tunnel (ngrok, cloudflared, Tailscale Funnel, nginx) adds. A browser and a
# local script never send these, so their presence means the request did not start on this machine.
FORWARDED_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
                     "cf-connecting-ip", "true-client-ip", "tailscale-user-login", "ngrok-trace-id")


def secret_configured() -> bool:
    return bool(config.secret(SECRET_NAME))


def authorised(presented: Optional[str]) -> bool:
    """Constant-time comparison with the configured secret. False when none is configured."""
    expected = config.secret(SECRET_NAME) or ""
    if not expected or not presented:
        return False
    return hmac.compare_digest(str(presented).encode(), expected.encode())


def allow_remote() -> bool:
    """The user's switch. Anything but an explicit yes is no."""
    from .. import settings
    try:
        return settings()["webhook"]["options"].get("allow_remote") is True
    except Exception:                                  # unreadable settings never OPEN a door
        return False


def forwarded(headers: dict) -> bool:
    return any(headers.get(name) for name in FORWARDED_HEADERS)


def _hostname(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):                           # [::1]:8140
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_local(client_host: Optional[str], headers: dict) -> bool:
    """From this machine, as far as a server can tell: loopback peer, local Host, nothing forwarded.
    Never true on a hosted install: there the proxy is the only way in."""
    if hosted.proxy_mode():
        return False
    peer = (client_host or "").strip().lower()
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = peer in LOCAL_CLIENTS
    return loopback and _hostname(headers.get("host", "")) in LOCAL_HOSTS and not forwarded(headers)


def reachable(client_host: Optional[str], headers: dict) -> bool:
    """May THIS request use the webhook at all (before the secret is even looked at)?"""
    return allow_remote() or is_local(client_host, headers)


class WebhookAdapter(Adapter):
    kind = "webhook"
    label = "Webhook"
    how = ("Have Zapier, Make or the recorder itself POST the transcript as JSON to /import/webhook with the "
           "header X-Salescoach-Secret. Off until a secret is set. The coach listens on this machine only, so a "
           "cloud automation needs a tunnel to reach it.")
    mode = "push"
    needs_key = True
    api_key_env = SECRET_NAME
    default_enabled = True
    default_poll_minutes = None

    def configured(self) -> bool:
        return secret_configured()

    def normalize(self, body: bytes) -> base.NormalizedTranscript:
        if len(body) > MAX_BYTES:
            raise SourceError("payload too large")
        try:
            payload = parsers.load_json(parsers.decode(body))
        except ValueError:
            raise parsers.UnrecognisedTranscript("the body is not JSON") from None
        parsed = parsers.parse_json(payload)
        if not parsed.turns:
            raise parsers.UnrecognisedTranscript("the payload has no turns")
        # trusted=False: the payload cannot mark turns as the seller's, and its id stays under `ext:`.
        return base.from_parsed(parsed, self.kind, raw=payload, source_ref=base.content_ref("webhook", body),
                                trusted=False)
