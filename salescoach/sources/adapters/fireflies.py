"""Fireflies.ai, through its GraphQL API (https://api.fireflies.ai/graphql, bearer API key).

UNVERIFIED: written against the public API documentation (docs.fireflies.ai: the `transcripts`
and `transcript` queries), exercised only with fixtures of the documented shape. Nothing here has
been run against the live service. The HTTP layer is `_post`; the ONLY code that knows the
response field names is parsers.fireflies_to_parsed (shared with the JSON export) and `_ref` below.
"""
from datetime import datetime, timezone
from typing import Optional

from ... import config
from .. import base, parsers
from . import Adapter, MeetingRef, SourceAuthError, SourceError
from ._http import request_json

URL = "https://api.fireflies.ai/graphql"
KEY = "FIREFLIES_API_KEY"
PAGE = 50                      # the documented maximum for `limit`
MAX_PAGES = 10

LIST_QUERY = """query Recent($limit: Int, $skip: Int, $fromDate: DateTime) {
  transcripts(limit: $limit, skip: $skip, fromDate: $fromDate) {
    id title date duration organizer_email participants
    meeting_attendees { displayName email name }
  }
}"""
FETCH_QUERY = """query One($id: String!) {
  transcript(id: $id) {
    id title date duration organizer_email participants
    meeting_attendees { displayName email name }
    sentences { index speaker_name text raw_text start_time end_time }
    summary { overview }
  }
}"""


def _ref(item: dict) -> MeetingRef:
    """One `transcripts` list item -> MeetingRef."""
    emails = [a.get("email") for a in item.get("meeting_attendees") or [] if isinstance(a, dict) and a.get("email")]
    emails += [p for p in item.get("participants") or [] if isinstance(p, str) and "@" in p]
    return MeetingRef(ext_id=str(item["id"]), source_ref=f"fireflies:{item['id']}", title=item.get("title") or "",
                      started_at=parsers.iso(item.get("date")), emails=list(dict.fromkeys(e.lower() for e in emails)))


def map_transcript(payload: dict) -> base.NormalizedTranscript:
    """The `transcript` object of a FETCH_QUERY answer -> NormalizedTranscript."""
    parsed = parsers.fireflies_to_parsed(payload)
    if not parsed.ext_id:
        raise SourceError("Fireflies answered without a transcript id")
    return base.from_parsed(parsed, "fireflies", raw=payload, title=parsed.title or "Fireflies meeting")


class FirefliesAdapter(Adapter):
    kind = "fireflies"
    label = "Fireflies.ai"
    how = ("Reads your recent Fireflies transcripts with your API key (Fireflies > Integrations > Fireflies API). "
           "Untested against the live API: if it fails, export the transcript as JSON and upload it.")
    needs_key = True
    api_key_env = KEY
    verified = False

    def __init__(self, options: Optional[dict] = None, transport=None):
        super().__init__(options)
        self._transport = transport

    def configured(self) -> bool:
        return config.has_secret(KEY)

    def _post(self, query: str, variables: dict) -> dict:
        key = config.secret(KEY)
        if not key:
            raise SourceAuthError("no Fireflies API key is set")
        answer = request_json("Fireflies", "POST", URL, {"Authorization": f"Bearer {key}"},
                              body={"query": query, "variables": variables}, transport=self._transport)
        errors = answer.get("errors") if isinstance(answer, dict) else None
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            code = str(first.get("code") or (first.get("extensions") or {}).get("code") or "")
            message = str(first.get("message") or "request failed")[:200]
            if "auth" in code.lower() or "unauthor" in message.lower() or "invalid api key" in message.lower():
                raise SourceAuthError("Fireflies refused the API key; set a new key")
            raise SourceError(f"Fireflies: {message}")
        return (answer or {}).get("data") or {}

    def list_recent(self, since: Optional[datetime] = None) -> list:
        variables = {"limit": PAGE, "skip": 0}
        if since:
            variables["fromDate"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        refs = []
        for _ in range(MAX_PAGES):
            page = self._post(LIST_QUERY, variables).get("transcripts") or []
            refs += [_ref(item) for item in page if isinstance(item, dict) and item.get("id")]
            if len(page) < PAGE:
                break
            variables = {**variables, "skip": variables["skip"] + PAGE}
        return refs

    def fetch(self, ext_id: str) -> base.NormalizedTranscript:
        payload = self._post(FETCH_QUERY, {"id": str(ext_id)}).get("transcript")
        if not isinstance(payload, dict):
            raise SourceError(f"Fireflies has no transcript {ext_id}")
        return map_transcript(payload)
