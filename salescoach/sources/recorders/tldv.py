"""tl;dv, per rep: the rep's own API key (tl;dv > Personal Settings > API Keys), x-api-key header, at
https://pasta.tldv.io/v1alpha1. Pro plans and above only (research table).

  list   GET /meetings?from=...&onlyParticipated=true&page=N&pageSize=50 -> {results: [...], page, pages}
         (onlyParticipated: the meetings the key's owner was in, not the whole workspace's)
  fetch  GET /meetings/{id} (organizer{email}, invitees[]) + GET /meetings/{id}/transcript
         -> {data: [{speaker, text, startTime, endTime}]}; a 404 on the transcript means "not ready yet"
  test   GET /meetings?onlyParticipated=true&pageSize=1 (the API has no "who am I"; "connected as" stays empty)

Speakers carry names only: the owner's aliases and remembered labels decide which one is the rep.

Webhook: MeetingReady / TranscriptReady events; the research notes give no signature scheme, so the
per-connection token header is required and the transcript is fetched with the rep's own key.

UNVERIFIED against the live API (verified=False).
"""
import re
from datetime import timezone
from typing import Optional

from .. import parsers
from ..adapters import SourceError
from ..adapters._http import request_json
from . import MAX_PAGES, Account, RecorderAdapter

BASE_URL = "https://pasta.tldv.io/v1alpha1"
PAGE = 50
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def to_parsed(meeting: dict, transcript) -> parsers.Parsed:
    """A tl;dv meeting and its transcript segments -> Parsed. The one place that knows tl;dv's field names."""
    items = transcript.get("data") if isinstance(transcript, dict) else transcript
    turns = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        who = item.get("speaker")
        label = (who.get("name") if isinstance(who, dict) else who) or ""
        turns.append(parsers._turn(label, item.get("text"), parsers.seconds(item.get("startTime")),
                                   parsers.seconds(item.get("endTime"))))
    people = parsers._people([meeting.get("organizer")] if isinstance(meeting.get("organizer"), dict) else [])
    known = {p["email"] for p in people if p["email"]}
    people += [p for p in parsers._people(meeting.get("invitees")) if not p["email"] or p["email"] not in known]
    ext_id = meeting.get("id")
    return parsers.Parsed("tldv", parsers.merge_runs(turns), title=parsers._scalar(meeting.get("name") or meeting.get("title")),
                          started_at=parsers.iso(meeting.get("happenedAt") or meeting.get("startTime")),
                          participants=people, ext_source="tldv", ext_id=str(ext_id) if ext_id else None)


class TldvRecorder(RecorderAdapter):
    kind = "tldv"
    label = "tl;dv"
    where_key = "tl;dv > Personal Settings > API Keys > Create API key."
    plan = "tl;dv Pro and above; the Free plan has no API."
    rate_note = ""
    default_poll_minutes = 15

    def _get(self, path: str, params: Optional[dict] = None):
        return request_json("tl;dv", "GET", BASE_URL + path, {"x-api-key": self._key}, params=params,
                            transport=self._transport)

    def test(self) -> Account:
        self._get("/meetings", {"onlyParticipated": "true", "pageSize": 1})
        return Account()

    def list_recent(self, since=None, cursor: Optional[str] = None) -> list:
        page_no = int(cursor) if (cursor or "").isdigit() and int(cursor) > 0 else 1
        params = {"onlyParticipated": "true", "pageSize": PAGE}
        if since:
            params["from"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        refs, self.cursor = [], None
        for n in range(MAX_PAGES):
            answer = self._get("/meetings", {**params, "page": page_no})
            answer = answer if isinstance(answer, dict) else {}
            for item in answer.get("results") or []:
                if not isinstance(item, dict) or not item.get("id") or not _ID.fullmatch(str(item["id"])):
                    continue
                people = [item.get("organizer")] + list(item.get("invitees") or [])
                refs.append(self.ref(item["id"], item.get("name") or "", parsers.iso(item.get("happenedAt")),
                                     [str(p.get("email")) for p in people if isinstance(p, dict) and p.get("email")]))
            pages = answer.get("pages")
            if not isinstance(pages, int) or page_no >= pages:
                break
            page_no += 1
            if n == MAX_PAGES - 1:
                self.cursor = str(page_no)
        return refs

    def fetch(self, ext_id: str):
        if not _ID.fullmatch(str(ext_id)):
            raise SourceError("not a tl;dv meeting id")
        meeting = self._get(f"/meetings/{ext_id}")
        meeting = meeting if isinstance(meeting, dict) else {}
        transcript = self._get(f"/meetings/{ext_id}/transcript")
        parsed = to_parsed({**meeting, "id": meeting.get("id") or ext_id}, transcript)
        self.mark_me(parsed, [self.account.name] if self.account.name else [])
        return self.normalized(parsed, ext_id, {"meeting": meeting, "transcript": transcript}, "tl;dv meeting")

    def webhook_event(self, payload) -> tuple:
        if not isinstance(payload, dict):
            return None, None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        ext_id = data.get("meetingId") or data.get("id")
        return (str(ext_id), None) if ext_id and _ID.fullmatch(str(ext_id)) else (None, None)
