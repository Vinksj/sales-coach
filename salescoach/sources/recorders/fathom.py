"""Fathom, per rep: the rep's own API key (Fathom > Settings > API Access), X-Api-Key header.

  list   GET https://api.fathom.ai/external/v1/meetings?include_transcript=true&created_after=...&cursor=...
         -> {items: [...], next_cursor}
  fetch  the listed item carries its transcript (include_transcript); a meeting not seen in this poll's
         listing (a webhook's) falls back to GET /recordings/{id}/transcript
  test   GET /meetings (the first page); "connected as" is recorded_by.email of a listed meeting

Speakers: each transcript item's speaker has display_name and matched_calendar_invitee_email; a speaker
whose address is the recorder's own (recorded_by.email, the rep) is the rep: channel 'me'. A listing that
also carries a colleague's shared recording (recorded_by someone else, the rep not on the invite) is
skipped: this connection delivers the rep's own meetings only.

Webhook: Fathom signs its webhooks (research: "HMAC-signed"); verified here as Standard Webhooks
(webhook-id / webhook-timestamp / webhook-signature) with the signing secret the rep pastes. The payload
is the meeting object, transcript included, and is imported as it stands.

UNVERIFIED against the live API (verified=False); parsers.fathom_to_parsed holds the field names.
"""
import re
from datetime import timezone
from typing import Optional

from .. import parsers
from ..adapters import SourceError
from ..adapters._http import request_json
from . import MAX_PAGES, Account, RecorderAdapter

BASE_URL = "https://api.fathom.ai/external/v1"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _email(obj) -> Optional[str]:
    value = obj.get("email") if isinstance(obj, dict) else None
    return str(value).strip().lower() if value and "@" in str(value) else None


class FathomRecorder(RecorderAdapter):
    kind = "fathom"
    label = "Fathom"
    where_key = "Fathom > Settings > API Access > Generate API key."
    plan = "Every Fathom plan, including Free."
    rate_note = "60 requests a minute."
    default_poll_minutes = 15
    webhook_scheme = "standard"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen: dict = {}                 # recording id -> the listed item (transcript included)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        answer = request_json("Fathom", "GET", BASE_URL + path, {"X-Api-Key": self._key}, params=params,
                              transport=self._transport)
        return answer if isinstance(answer, dict) else {}

    def test(self) -> Account:
        page = self._get("/meetings")
        for item in page.get("items") or []:
            mine = _email(item.get("recorded_by")) if isinstance(item, dict) else None
            if mine:
                name = (item.get("recorded_by") or {}).get("name")
                return Account(email=mine, name=name or None)
        return Account()

    def _mine(self, item: dict) -> bool:
        """The account's own meeting: recorded by the account, or the account was invited. Unknown account:
        everything the key lists."""
        me = (self.account.email or "").lower()
        if not me:
            return True
        if _email(item.get("recorded_by")) == me:
            return True
        return any(_email(p) == me for p in item.get("calendar_invitees") or [])

    def list_recent(self, since=None, cursor: Optional[str] = None) -> list:
        params = {"include_transcript": "true"}
        if since:
            params["created_after"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if cursor:
            params["cursor"] = cursor
        refs, self.cursor = [], None
        for page_no in range(MAX_PAGES):
            page = self._get("/meetings", params)
            for item in page.get("items") or []:
                if not isinstance(item, dict) or not (item.get("recording_id") or item.get("id")):
                    continue
                if not self._mine(item):
                    continue
                rid = str(item.get("recording_id") or item.get("id"))
                self._seen[rid] = item
                people = list(item.get("calendar_invitees") or []) + [item.get("recorded_by") or {}]
                refs.append(self.ref(rid, item.get("title") or item.get("meeting_title") or "",
                                     parsers.iso(item.get("recording_start_time") or item.get("scheduled_start_time")
                                                 or item.get("created_at")),
                                     [e for e in (_email(p) for p in people) if e]))
            nxt = page.get("next_cursor")
            if not nxt:
                break
            params = {**params, "cursor": nxt}
            if page_no == MAX_PAGES - 1:
                self.cursor = str(nxt)                  # capped: the next poll resumes here
        return refs

    def map(self, meeting: dict):
        parsed = parsers.fathom_to_parsed(meeting)
        if not parsed.ext_id:
            raise SourceError("Fathom answered without a recording id")
        me = {self.account.email or "", _email(meeting.get("recorded_by")) or ""} - {""}
        labels = []
        for item in meeting.get("transcript") or []:
            who = item.get("speaker") if isinstance(item, dict) else None
            if isinstance(who, dict) and str(who.get("matched_calendar_invitee_email") or "").lower() in me:
                labels.append(who.get("display_name") or who.get("name") or "")
        self.mark_me(parsed, labels)
        return self.normalized(parsed, parsed.ext_id, {"meeting": meeting}, "Fathom meeting")

    def fetch(self, ext_id: str):
        if not _ID.fullmatch(str(ext_id)):
            raise SourceError("not a Fathom recording id")
        meeting = dict(self._seen.get(str(ext_id)) or {"recording_id": ext_id})
        if not isinstance(meeting.get("transcript"), list):
            meeting["transcript"] = self._get(f"/recordings/{ext_id}/transcript").get("transcript") or []
        meeting["recording_id"] = meeting.get("recording_id") or ext_id
        return self.map(meeting)

    def webhook_event(self, payload) -> tuple:
        if not isinstance(payload, dict):
            return None, None
        meeting = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        ext_id = meeting.get("recording_id") or meeting.get("id")
        if not ext_id or not _ID.fullmatch(str(ext_id)):
            return None, None
        if isinstance(meeting.get("transcript"), list) and meeting["transcript"]:
            return str(ext_id), self.map({**meeting, "recording_id": ext_id})
        return str(ext_id), None
