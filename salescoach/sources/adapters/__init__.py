"""Source adapters: how a transcript gets from a recorder to sources/base.import_normalized.

Three kinds of path:
  push   the transcript is handed to us        upload (the /import page), webhook (POST /import/webhook)
  poll   we go and look                        folder (data/inbox/drop), fireflies, fathom, granola
  export the recorder has no public API        Otter, tl;dv, Zoom, Meet, Teams, Gong, ...: export the
                                               transcript, then Upload or Folder (or Zapier/Make -> webhook)

Every adapter is a SourceAdapter. A poll adapter lists MeetingRefs and fetches one as a
NormalizedTranscript; the poller (salescoach.sources.poll) owns dedupe, deal mapping, the import
and the per-source state, so an adapter is only "list" and "fetch". Adapters never write the
database and never see a secret's value outside the request they put it in.

`verified=False` means the adapter was written against the service's public API documentation and
exercised only with recorded-shape fixtures: the UI and docs/sources.md say "untested against the
live API" for those.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from .. import parsers
from ..base import NormalizedTranscript

MAX_BYTES = parsers.MAX_INPUT_BYTES   # one transcript, by any path; a three-hour call is well under 1 MB of text


class SourceError(RuntimeError):
    """An adapter could not list or fetch. The message never contains a key."""


class SourceAuthError(SourceError):
    """The service refused the key."""


class SourceNotFound(SourceError):
    """HTTP 404: no such meeting, or (for a recorder that lists a meeting before its transcript is done)
    not there YET. A per-rep poller retries it on a later poll while the meeting is recent."""


class SourceRateLimited(SourceError):
    """The service said "too many requests" (HTTP 429, or a GraphQL error saying so). retry_after is the
    seconds it asked for when it said, else None. The poller backs off and remembers until when."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class MeetingRef:
    ext_id: str                                        # the meeting's id at the source
    source_ref: str                                    # what calls.source_ref will be: the dedupe key
    title: str = ""
    started_at: Optional[str] = None
    emails: list = field(default_factory=list)         # participant addresses, for the deal mapping


@runtime_checkable
class SourceAdapter(Protocol):
    kind: str                 # calls.source and the key in sources.yaml
    label: str                # what the setup UI calls it
    how: str                  # one or two sentences: how it works, what the user has to do
    mode: str                 # push | poll
    needs_key: bool
    api_key_env: Optional[str]    # the config.secret() name the key is stored under
    verified: bool            # False: untested against the live API
    default_enabled: bool
    default_poll_minutes: Optional[int]

    def configured(self) -> bool: ...
    def list_recent(self, since: Optional[datetime]) -> list: ...
    def fetch(self, ext_id: str) -> NormalizedTranscript: ...


class Adapter:
    """Defaults shared by the adapters. `options` is this adapter's block from sources.yaml."""
    kind = ""
    label = ""
    how = ""
    mode = "poll"
    needs_key = False
    api_key_env = None
    verified = True
    default_enabled = False
    default_poll_minutes = 15

    def __init__(self, options: Optional[dict] = None):
        self.options = dict(options or {})

    # An adapter never holds a database handle. What it must remember between polls (the folder's
    # set-aside files) goes through these two, which the poller points at the state table under
    # sources:<kind>:<key>. Unbound (a test, the CLI building an adapter by hand) they keep it in memory.
    _memory: Optional[dict] = None
    _state_get = None
    _state_set = None

    def use_state(self, get, set_) -> None:
        self._state_get, self._state_set = get, set_

    def state_get(self, key: str):
        if self._state_get is not None:
            return self._state_get(key)
        return (self._memory or {}).get(key)

    def state_set(self, key: str, value: str) -> None:
        if self._state_set is not None:
            self._state_set(key, value)
            return
        if self._memory is None:
            self._memory = {}
        self._memory[key] = value

    def configured(self) -> bool:
        return True

    def list_recent(self, since=None) -> list:
        return []

    def fetch(self, ext_id: str) -> NormalizedTranscript:
        raise SourceError(f"{self.kind} does not fetch: transcripts are pushed to it")

    # Hooks for adapters that own something outside the database (the folder moves its files).
    def after_import(self, ext_id: str, result) -> None:
        pass

    def after_failure(self, ext_id: str, error: str, exc: Optional[BaseException] = None) -> None:
        pass


# Recorders with no documented public API for transcripts (checked 2026-09): nothing is invented for
# them. They all export, and every export format is one the parsers read.
EXPORT_ONLY = [
    ("otter", "Otter", "Open the conversation, Export, Text (.txt) or .srt. Upload it on the Import page or save it "
                       "into the watched folder. Otter's Zapier trigger can post to the webhook instead."),
    ("tldv", "tl;dv", "Download the transcript from the meeting page and upload it, or use tl;dv's Zapier/Make "
                      "integration to post to the webhook."),
    ("zoom", "Zoom", "Cloud recordings carry an audio transcript (.vtt): download it and upload it, or drop it "
                     "into the watched folder."),
    ("meet", "Google Meet", "Meet saves the transcript as a Google Doc: download it as plain text (.txt) and upload it."),
    ("teams", "Microsoft Teams", "Download the transcript from the meeting recap as .vtt and upload it."),
    ("gong", "Gong", "Export the call transcript and upload it. (Gong's API is for enterprise admins; no adapter "
                     "is shipped.)"),
    ("other", "Any other recorder", "Export the transcript as text, .vtt, .srt or JSON and use Upload or the watched "
                                    "folder; or have Zapier / Make post the generic JSON to the webhook."),
]


def classes() -> dict:
    from .fathom import FathomAdapter
    from .fireflies import FirefliesAdapter
    from .folder import FolderAdapter
    from .granola import GranolaAdapter
    from .upload import UploadAdapter
    from .webhook import WebhookAdapter
    return {c.kind: c for c in (UploadAdapter, FolderAdapter, WebhookAdapter, FirefliesAdapter, FathomAdapter,
                                GranolaAdapter)}


def build(kind: str, options: Optional[dict] = None) -> Adapter:
    try:
        return classes()[kind](options)
    except KeyError:
        raise SourceError(f"unknown source {kind!r}; one of {', '.join(classes())}") from None
