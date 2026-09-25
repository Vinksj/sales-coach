"""Per-rep recorder adapters (Phase 4): one rep's OWN recorder account, read with that rep's own key.

A call belongs to the user whose connection delivered it (plan, Approach 4). Each rep connects their
own Fathom / Fireflies / tl;dv / Granola account on /me/setup; the adapter here lists THAT account's
meetings and fetches their transcripts, and every reference it makes embeds the owner:

    source_ref = "<kind>:<owner user id>:<id at the recorder>"

so two reps who were on the same meeting each get their own call from their own account, analysed from
their own side (turns.channel 'me' is that rep; a colleague is just another speaker). Nothing here
matches organisers, merges duplicates or assigns a call to anyone: the owner is fixed when the adapter
is built, from the connection row, and no payload can change it.

The interface (sources/connections.py drives it):

    cls(api_key, owner, transport=None, account=None)
    test() -> Account                      one cheap authenticated call; the account's address when the API says
    list_recent(since, cursor=None) -> [MeetingRef]   that account's recent meetings; self.cursor = where a
                                           capped listing stopped (resumed next poll), else None
    fetch(ext_id) -> NormalizedTranscript  source_ref embeds the owner; the rep's own turns marked 'me' where the
                                           recorder says so (Fathom's speaker e-mail, Granola's attribution)
    webhook_event(payload) -> (ext_id | None, NormalizedTranscript | None)
                                           a push notification: the meeting id it announces, and the transcript
                                           itself when the payload embeds one

Every adapter here is verified=False: written from the vendors' public API documentation (the research
notes, plans/sales-coach-cloud-research.md, "Recorder APIs for the per-rep model") and exercised only with
recorded-shape fixtures over httpx.MockTransport. No live API was called. Keys never appear in an error:
the HTTP layer is adapters/_http.request_json.
"""
from dataclasses import dataclass
from typing import Optional

from .. import base, parsers
from ..adapters import MeetingRef, SourceError  # noqa: F401  (re-exported for the adapters)

MAX_PAGES = 10


@dataclass
class Account:
    """Who the key belongs to, when the API says (Fireflies' `user`; the owner of a listed Fathom or
    Granola meeting). Shown on the card as "connected as ..."; never used to decide ownership."""
    email: Optional[str] = None
    name: Optional[str] = None


class RecorderAdapter:
    kind = ""
    label = ""
    where_key = ""               # one line: where the rep finds their key
    plan = ""                    # which plans have API access (research table)
    rate_note = ""               # the documented rate limit, for the card and the docs
    default_poll_minutes = 15
    webhook_scheme = "header"    # "standard": the recorder signs (Standard Webhooks HMAC); else our own token header
    verified = False

    def __init__(self, api_key: str, owner: str, transport=None, account: Optional[Account] = None):
        if not api_key:
            from ..adapters import SourceAuthError
            raise SourceAuthError(f"no {self.label} API key is stored for this connection")
        if not owner:
            raise ValueError("a recorder adapter needs its owner")
        self._key = api_key
        self.owner = owner
        self._transport = transport
        self.account = account or Account()
        self.cursor: Optional[str] = None

    def __repr__(self):                       # never the key
        return f"<{type(self).__name__} owner={self.owner!r}>"

    # ---- what every adapter shares -------------------------------------------------------------

    def source_ref(self, ext_id) -> str:
        return f"{self.kind}:{self.owner}:{ext_id}"

    def ref(self, ext_id, title="", started_at=None, emails=()) -> MeetingRef:
        return MeetingRef(ext_id=str(ext_id), source_ref=self.source_ref(ext_id), title=title or "",
                          started_at=started_at, emails=list(dict.fromkeys(e.lower() for e in emails if e)))

    def normalized(self, parsed: parsers.Parsed, ext_id, raw, title_default: str) -> base.NormalizedTranscript:
        """A Parsed as the owner's NormalizedTranscript: the ref embeds the owner whatever the payload says."""
        return base.NormalizedTranscript(source_kind=self.kind, source_ref=self.source_ref(ext_id),
                                         title=parsed.title or title_default, started_at=parsed.started_at,
                                         ended_at=parsed.ended_at, participants=list(parsed.participants),
                                         turns=list(parsed.turns), summary=parsed.summary, raw=raw)

    @staticmethod
    def mark_me(parsed: parsers.Parsed, me_labels) -> parsers.Parsed:
        """Set channel 'me' on the turns whose label the recorder itself tied to the account holder. Other
        turns keep no channel: the owner's aliases and remembered labels decide them (sources/base.map_speakers)."""
        keys = {base.norm_label(label) for label in me_labels if base.norm_label(label)}
        if keys:
            for turn in parsed.turns:
                if base.norm_label(turn.get("speaker_label")) in keys:
                    turn["channel"] = "me"
        return parsed

    def test(self) -> Account:
        raise NotImplementedError

    def list_recent(self, since=None, cursor: Optional[str] = None) -> list:
        raise NotImplementedError

    def fetch(self, ext_id: str) -> base.NormalizedTranscript:
        raise NotImplementedError

    def webhook_event(self, payload) -> tuple:
        return None, None


def classes() -> dict:
    from .fathom import FathomRecorder
    from .fireflies import FirefliesRecorder
    from .granola import GranolaRecorder
    from .tldv import TldvRecorder
    return {c.kind: c for c in (FathomRecorder, FirefliesRecorder, TldvRecorder, GranolaRecorder)}


def build(kind: str, api_key: str, owner: str, transport=None, account: Optional[Account] = None) -> RecorderAdapter:
    try:
        cls = classes()[kind]
    except KeyError:
        raise SourceError(f"unknown recorder {kind!r}; one of {', '.join(classes())}") from None
    return cls(api_key, owner, transport=transport, account=account)


# Recorders whose APIs are org-level only (an admin key over everyone's calls): no per-rep connection is
# possible, so none is offered in cloud mode. docs/sources.md says the same.
ORG_LEVEL_ONLY = ("Gong", "Otter", "Avoma", "Chorus")
LATER = ("Zoom", "Google Meet")
