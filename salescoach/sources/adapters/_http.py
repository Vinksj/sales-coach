"""The thin HTTP layer of the API adapters: one request, explicit timeouts, no redirects, and no
key in any error. Exceptions are raised `from None` so the httpx request (whose headers carry the
key) is never chained onto them."""
from typing import Optional

import httpx

from . import SourceAuthError, SourceError, SourceNotFound, SourceRateLimited

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


def request_json(service: str, method: str, url: str, headers: dict, params: Optional[dict] = None,
                 body: Optional[dict] = None, transport=None):
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=transport) as client:
            response = client.request(method, url, headers=headers, params=params, json=body)
    except httpx.TimeoutException:
        raise SourceError(f"{service} did not answer in time") from None
    except httpx.HTTPError as exc:
        raise SourceError(f"{service} could not be reached: {type(exc).__name__}") from None
    if response.status_code in (401, 403):
        raise SourceAuthError(f"{service} refused the API key (HTTP {response.status_code}); set a new key")
    if response.status_code == 429:
        raise SourceRateLimited(f"{service} is rate limiting; the next poll will try again",
                                retry_after=retry_after(response.headers.get("retry-after")))
    if response.status_code == 404:
        raise SourceNotFound(f"{service} answered HTTP 404")
    if response.status_code >= 300:
        raise SourceError(f"{service} answered HTTP {response.status_code}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise SourceError(f"{service} sent an implausibly large response")
    try:
        return response.json()
    except ValueError:
        raise SourceError(f"{service} did not answer with JSON") from None


def retry_after(value) -> Optional[float]:
    """Seconds from a Retry-After header (a number of seconds or an HTTP date); None when absent or unreadable."""
    if value in (None, ""):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    try:
        when = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
