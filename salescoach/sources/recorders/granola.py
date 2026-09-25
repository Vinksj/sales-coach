"""Granola, per rep: a personal API key (Granola > Settings > Connectors > API keys; Business and
Enterprise plans), bearer auth, at https://public-api.granola.ai/v1. In a cloud install this replaces
the `claude -p` connector path, which is one machine's (sources/adapters/granola.py stays local-only).

  list   GET /v1/notes?created_after=...&cursor=...&page_size=30 -> {notes: [...], hasMore, cursor}
  fetch  GET /v1/notes/{id}?include=transcript: owner{email}, attendees[].email, and the transcript, each
         entry's speaker.attribution 'me' | 'them' (Granola separates the rep's microphone from the call
         audio), which maps the rep's own turns directly to channel 'me'
  test   GET /v1/notes?page_size=1; "connected as" is the listed note's owner.email

Webhook: `note.generated`, signed as Standard Webhooks (webhook-id / webhook-timestamp /
webhook-signature, HMAC-SHA256 with the signing secret the rep pastes). The payload names the note; the
transcript is fetched with the rep's own key.

UNVERIFIED against the live API (verified=False).
"""
import re
from datetime import timezone
from typing import Optional

from .. import parsers
from ..adapters import SourceError
from ..adapters._http import request_json
from . import MAX_PAGES, Account, RecorderAdapter

BASE_URL = "https://public-api.granola.ai/v1"
PAGE = 30
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _attribution(speaker) -> Optional[str]:
    if isinstance(speaker, dict):
        value = str(speaker.get("attribution") or "").strip().lower()
        if value in ("me", "them"):
            return value
        source = str(speaker.get("source") or "").strip().lower()
        return {"microphone": "me", "speaker": "them", "system": "them"}.get(source)
    return None


def to_parsed(note: dict) -> parsers.Parsed:
    """A Granola note with its transcript -> Parsed. The one place that knows Granola's API field names."""
    turns = []
    for item in note.get("transcript") or []:
        if not isinstance(item, dict):
            continue
        who = item.get("speaker")
        channel = _attribution(who)
        label = (who.get("name") or who.get("label") if isinstance(who, dict) else who) or ""
        label = label or ("Me" if channel == "me" else "Them" if channel == "them" else "")
        turns.append(parsers._turn(label, item.get("text"), parsers.seconds(item.get("start_time")),
                                   parsers.seconds(item.get("end_time")), channel=channel))
    event = note.get("calendar_event") if isinstance(note.get("calendar_event"), dict) else {}
    people = parsers._people(note.get("attendees") or event.get("attendees") or event.get("invitees"))
    summary = note.get("summary_text") or note.get("summary_markdown") or note.get("summary")
    ext_id = note.get("id")
    return parsers.Parsed("granola", parsers.merge_runs(turns), title=parsers._scalar(note.get("title") or event.get("event_title")),
                          started_at=parsers.iso(event.get("scheduled_start_time") or note.get("created_at")),
                          ended_at=parsers.iso(event.get("scheduled_end_time")), participants=people,
                          summary=summary if isinstance(summary, str) else None,
                          ext_source="granola", ext_id=str(ext_id) if ext_id else None)


class GranolaRecorder(RecorderAdapter):
    kind = "granola"
    label = "Granola"
    where_key = "Granola > Settings > Connectors > API keys > Create key (Business plan)."
    plan = "Granola Business or Enterprise; personal keys are not on the free or individual plans."
    rate_note = ""
    default_poll_minutes = 15
    webhook_scheme = "standard"

    def _get(self, path: str, params: Optional[dict] = None):
        return request_json("Granola", "GET", BASE_URL + path, {"Authorization": f"Bearer {self._key}"},
                            params=params, transport=self._transport)

    def test(self) -> Account:
        answer = self._get("/notes", {"page_size": 1})
        notes = answer.get("notes") if isinstance(answer, dict) else None
        owner = notes[0].get("owner") if notes and isinstance(notes[0], dict) else None
        if isinstance(owner, dict) and owner.get("email"):
            return Account(email=str(owner["email"]).lower(), name=owner.get("name") or None)
        return Account()

    def list_recent(self, since=None, cursor: Optional[str] = None) -> list:
        params = {"page_size": PAGE}
        if since:
            params["created_after"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if cursor:
            params["cursor"] = cursor
        refs, self.cursor = [], None
        for n in range(MAX_PAGES):
            answer = self._get("/notes", params)
            answer = answer if isinstance(answer, dict) else {}
            for note in answer.get("notes") or []:
                if not isinstance(note, dict) or not note.get("id") or not _ID.fullmatch(str(note["id"])):
                    continue
                event = note.get("calendar_event") if isinstance(note.get("calendar_event"), dict) else {}
                people = list(note.get("attendees") or event.get("attendees") or [])
                refs.append(self.ref(note["id"], note.get("title") or event.get("event_title") or "",
                                     parsers.iso(event.get("scheduled_start_time") or note.get("created_at")),
                                     [str(p.get("email")) for p in people if isinstance(p, dict) and p.get("email")]))
            nxt = answer.get("cursor") if answer.get("hasMore", answer.get("has_more")) else None
            if not nxt:
                break
            params = {**params, "cursor": nxt}
            if n == MAX_PAGES - 1:
                self.cursor = str(nxt)
        return refs

    def fetch(self, ext_id: str):
        if not _ID.fullmatch(str(ext_id)):
            raise SourceError("not a Granola note id")
        note = self._get(f"/notes/{ext_id}", {"include": "transcript"})
        note = note if isinstance(note, dict) else {}
        parsed = to_parsed({**note, "id": note.get("id") or ext_id})
        if not parsed.turns:
            from ..adapters import SourceNotFound
            raise SourceNotFound(f"Granola note {ext_id} has no transcript (yet)")
        return self.normalized(parsed, ext_id, note, "Granola meeting")

    def webhook_event(self, payload) -> tuple:
        if not isinstance(payload, dict):
            return None, None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        note = data.get("note") if isinstance(data.get("note"), dict) else data
        ext_id = note.get("id") or data.get("note_id")
        return (str(ext_id), None) if ext_id and _ID.fullmatch(str(ext_id)) else (None, None)
