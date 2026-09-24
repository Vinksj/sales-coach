"""Import a text transcript (pasted, or exported from a recorder) as a call.

Accepted shapes, which can be mixed:
  one turn per line          "Me: ...", "Them: ...", "Anita Desai: ..."
  Granola's single string    " Them: Sir. Good afternoon.  Me: ..."
The splitting lives in sources/parsers.py and the import itself in sources/base.py (the one path
every transcript takes); this module is the paste box's thin front.

'Me' is the seller, and so is a label that is the seller's name or alias (base.map_speakers);
every other label is the buyer side, kept as the speaker cluster so the review UI can map it to
a person. Pasted text has no recogniser confidence, so the quality agent grades every turn itself.

Re-pasting the same transcript returns the call it already made: the source_ref is a digest of
the parsed turns, so a changed title or stray blank lines do not create a second call.
"""
import hashlib

from . import base, parsers

LABEL = parsers._OLD_LABEL              # kept for callers that imported the patterns from here
STRICT_LABEL = parsers.STRICT_LABEL


def parse(text: str, strict: bool = False) -> list[tuple[str, str, str]]:
    """Return (channel, cluster, text) turns. Only the literal label "Me" is the seller here: this
    is the label-level view. import_text resolves the seller's own name through the profile."""
    turns = []
    for label, body, _ in parsers.labelled(text, strict=strict):
        if label.lower() == "me":
            turns.append(("me", "me", body))
        else:
            turns.append(("them", "them_1" if label.lower() == "them" else label, body))
    return turns


def text_ref(turns, conn=None) -> str:
    """paste:<owner>:<sha of the normalised text>: labels and words only, whitespace collapsed. The owner
    is in the key so the same text pasted by two users is two calls, and one user's re-paste is a no-op."""
    from .. import repo
    lines = [f"{' '.join(t['speaker_label'].split()).casefold()}|{' '.join(t['text'].split())}" for t in turns]
    return repo.user_source_ref("paste", hashlib.sha256("\n".join(lines).encode()).hexdigest()[:32], conn)


def import_text(conn, text, title, deal_id=None, started_at=None, lang_mode="auto",
                participants=(), source="paste", source_ref=None, strict=False, history=False,
                me_label=None, result=False):
    """-> call id (or the base.ImportResult with result=True). `participants` are people ids.
    strict: only Me / Them / Speaker X are labels (a recorder that never prints names)."""
    parsed = parsers.parse_plain(text, strict=strict)
    if not parsed.turns:
        raise ValueError("no speaker turns found; expected lines like 'Me: ...' and 'Them: ...'")
    nt = base.NormalizedTranscript(source_kind=source, source_ref=source_ref or text_ref(parsed.turns, conn),
                                   title=title, started_at=started_at, ended_at=started_at,
                                   turns=parsed.turns, raw=text)
    outcome = base.import_normalized(conn, nt, deal_id=deal_id, history=history, lang_mode=lang_mode,
                                     me_label=me_label, participant_ids=tuple(participants))
    return outcome if result else outcome.call_id
