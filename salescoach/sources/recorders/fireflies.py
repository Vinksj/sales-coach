"""Fireflies.ai, per rep: the rep's own API key (Fireflies > Settings > Developer settings), bearer auth,
GraphQL at https://api.fireflies.ai/graphql.

  list   transcripts(mine: true, fromDate, limit <= 50, skip): only the key owner's own meetings
  fetch  transcript(id): sentences (speaker_name, text, start/end), organizer_email, host_email,
         meeting_attendees[].email
  test   user { email name }: the key's owner, shown as "connected as"

Speakers: a sentence's speaker_name equal to the account holder's name (from `user`) is the rep ('me');
the rest go through the owner's aliases and remembered labels like any transcript.

Rate limits (research table): Free 50 requests a day, Pro 500 a day, Business 60 a minute. A 429, or a
GraphQL error that says "too many requests", raises SourceRateLimited; the poller then waits (the
server's Retry-After, else an hour) and remembers until when in the connection row. The default poll is
hourly, so a Free account spends about 24 requests a day on listing.

Webhook: Fireflies posts {meetingId, eventType} to a per-user URL; the research notes give no signature
scheme, so the per-connection token header is required, and the transcript is then fetched with the rep's
own key (the payload is only a notification).

UNVERIFIED against the live API (verified=False); parsers.fireflies_to_parsed holds the field names.
"""
import re
from datetime import timezone
from typing import Optional

from .. import parsers
from ..adapters import SourceAuthError, SourceError, SourceRateLimited
from ..adapters._http import request_json
from . import MAX_PAGES, Account, RecorderAdapter

URL = "https://api.fireflies.ai/graphql"
PAGE = 50                      # the documented maximum for `limit`
RATE_LIMIT_WAIT_S = 3600
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

USER_QUERY = "query Me { user { email name } }"
LIST_QUERY = """query Mine($limit: Int, $skip: Int, $fromDate: DateTime) {
  transcripts(mine: true, limit: $limit, skip: $skip, fromDate: $fromDate) {
    id title date duration organizer_email host_email participants
    meeting_attendees { displayName email name }
  }
}"""
FETCH_QUERY = """query One($id: String!) {
  transcript(id: $id) {
    id title date duration organizer_email host_email participants
    meeting_attendees { displayName email name }
    sentences { index speaker_name text raw_text start_time end_time }
    summary { overview }
  }
}"""


class FirefliesRecorder(RecorderAdapter):
    kind = "fireflies"
    label = "Fireflies.ai"
    where_key = "Fireflies > Settings > Developer settings > API key."
    plan = "Every Fireflies plan; Free allows 50 API requests a day, Pro 500 a day, Business 60 a minute."
    rate_note = "Free 50 requests a day, Pro 500 a day, Business 60 a minute."
    default_poll_minutes = 60

    def _post(self, query: str, variables: Optional[dict] = None) -> dict:
        answer = request_json("Fireflies", "POST", URL, {"Authorization": f"Bearer {self._key}"},
                              body={"query": query, "variables": variables or {}}, transport=self._transport)
        errors = answer.get("errors") if isinstance(answer, dict) else None
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            code = str(first.get("code") or (first.get("extensions") or {}).get("code") or "").lower()
            message = str(first.get("message") or "request failed")[:200]
            low = message.lower()
            if "too_many" in code or "rate" in code or "too many requests" in low or "rate limit" in low:
                raise SourceRateLimited("Fireflies is rate limiting this account; the next poll waits", RATE_LIMIT_WAIT_S)
            if "auth" in code or "unauthor" in low or "invalid api key" in low or "forbidden" in code:
                raise SourceAuthError("Fireflies refused the API key")
            raise SourceError(f"Fireflies: {message}")
        return (answer or {}).get("data") or {}

    def test(self) -> Account:
        user = self._post(USER_QUERY).get("user") or {}
        email = str(user.get("email") or "").strip().lower() or None
        return Account(email=email if email and "@" in email else None, name=str(user.get("name") or "").strip() or None)

    def list_recent(self, since=None, cursor: Optional[str] = None) -> list:
        variables = {"limit": PAGE, "skip": int(cursor) if (cursor or "").isdigit() else 0}
        if since:
            variables["fromDate"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        refs, self.cursor = [], None
        for page_no in range(MAX_PAGES):
            page = self._post(LIST_QUERY, variables).get("transcripts") or []
            for item in page:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                emails = [a.get("email") for a in item.get("meeting_attendees") or [] if isinstance(a, dict)]
                emails += [p for p in item.get("participants") or [] if isinstance(p, str) and "@" in p]
                emails += [item.get("organizer_email"), item.get("host_email")]
                refs.append(self.ref(item["id"], item.get("title") or "", parsers.iso(item.get("date")),
                                     [str(e) for e in emails if e and "@" in str(e)]))
            if len(page) < PAGE:
                break
            variables = {**variables, "skip": variables["skip"] + PAGE}
            if page_no == MAX_PAGES - 1:
                self.cursor = str(variables["skip"])
        return refs

    def map(self, payload: dict):
        parsed = parsers.fireflies_to_parsed(payload)
        if not parsed.ext_id:
            raise SourceError("Fireflies answered without a transcript id")
        self.mark_me(parsed, [self.account.name] if self.account.name else [])
        return self.normalized(parsed, parsed.ext_id, payload, "Fireflies meeting")

    def fetch(self, ext_id: str):
        if not _ID.fullmatch(str(ext_id)):
            raise SourceError("not a Fireflies transcript id")
        payload = self._post(FETCH_QUERY, {"id": str(ext_id)}).get("transcript")
        if not isinstance(payload, dict):
            from ..adapters import SourceNotFound
            raise SourceNotFound(f"Fireflies has no transcript {ext_id} (yet)")
        return self.map(payload)

    def webhook_event(self, payload) -> tuple:
        if not isinstance(payload, dict):
            return None, None
        ext_id = payload.get("meetingId") or payload.get("meeting_id") or payload.get("transcriptId")
        return (str(ext_id), None) if ext_id and _ID.fullmatch(str(ext_id)) else (None, None)
