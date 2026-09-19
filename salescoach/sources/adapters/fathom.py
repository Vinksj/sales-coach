"""Fathom, through its REST API (https://api.fathom.ai/external/v1, X-Api-Key header).

UNVERIFIED: written against the public API documentation (developers.fathom.ai: `GET /meetings`
with cursor pagination, `GET /recordings/{id}/transcript`), exercised only with fixtures of the
documented shape. Nothing here has been run against the live service. The HTTP layer is `_get`;
the ONLY code that knows the response field names is parsers.fathom_to_parsed (shared with the
JSON export) and `_ref` below.
"""
import re
from datetime import datetime, timezone
from typing import Optional

from ... import config
from .. import base, parsers
from . import Adapter, MeetingRef, SourceAuthError, SourceError
from ._http import request_json

BASE_URL = "https://api.fathom.ai/external/v1"
KEY = "FATHOM_API_KEY"
MAX_PAGES = 10


def _ref(item: dict) -> MeetingRef:
    """One `meetings` list item -> MeetingRef."""
    rid = str(item.get("recording_id") or item.get("id"))
    people = list(item.get("calendar_invitees") or [])
    if isinstance(item.get("recorded_by"), dict):
        people.append(item["recorded_by"])
    emails = [p.get("email") for p in people if isinstance(p, dict) and p.get("email")]
    return MeetingRef(ext_id=rid, source_ref=f"fathom:{rid}", title=item.get("title") or item.get("meeting_title") or "",
                      started_at=parsers.iso(item.get("recording_start_time") or item.get("scheduled_start_time")
                                             or item.get("created_at")),
                      emails=list(dict.fromkeys(e.lower() for e in emails)))


def map_meeting(meeting: dict, transcript: list) -> base.NormalizedTranscript:
    """A `meetings` item plus the recording's transcript list -> NormalizedTranscript."""
    parsed = parsers.fathom_to_parsed({**meeting, "transcript": transcript})
    if not parsed.ext_id:
        raise SourceError("Fathom answered without a recording id")
    return base.from_parsed(parsed, "fathom", raw={"meeting": meeting, "transcript": transcript},
                            title=parsed.title or "Fathom meeting")


class FathomAdapter(Adapter):
    kind = "fathom"
    label = "Fathom"
    how = ("Reads your recent Fathom meetings with your API key (Fathom > Settings > API Access). Untested against "
           "the live API: if it fails, copy the transcript from Fathom and upload it.")
    needs_key = True
    api_key_env = KEY
    verified = False

    def __init__(self, options: Optional[dict] = None, transport=None):
        super().__init__(options)
        self._transport = transport
        self._seen: dict = {}              # recording id -> the list item, so fetch() has the meeting's metadata

    def configured(self) -> bool:
        return config.has_secret(KEY)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        key = config.secret(KEY)
        if not key:
            raise SourceAuthError("no Fathom API key is set")
        answer = request_json("Fathom", "GET", BASE_URL + path, {"X-Api-Key": key}, params=params,
                              transport=self._transport)
        return answer if isinstance(answer, dict) else {}

    def list_recent(self, since: Optional[datetime] = None) -> list:
        params = {}
        if since:
            params["created_after"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        refs = []
        for _ in range(MAX_PAGES):
            page = self._get("/meetings", params)
            for item in page.get("items") or []:
                if isinstance(item, dict) and (item.get("recording_id") or item.get("id")):
                    ref = _ref(item)
                    self._seen[ref.ext_id] = item
                    refs.append(ref)
            cursor = page.get("next_cursor")
            if not cursor:
                break
            params = {**params, "cursor": cursor}
        return refs

    def fetch(self, ext_id: str) -> base.NormalizedTranscript:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(ext_id)):
            raise SourceError("not a Fathom recording id")
        meeting = self._seen.get(str(ext_id)) or {"recording_id": ext_id}
        transcript = meeting.get("transcript")
        if not isinstance(transcript, list):
            transcript = self._get(f"/recordings/{ext_id}/transcript").get("transcript") or []
        return map_meeting({**meeting, "recording_id": meeting.get("recording_id") or ext_id}, transcript)
