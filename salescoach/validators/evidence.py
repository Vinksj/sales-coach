"""Evidence checks: the guard against hallucinated commitments.

An agent may cite turns and quote words. Before anything it says is stored as
more than a low-confidence inference, the quote has to be found in the cited
turns. Rules, several learned from the 2026-09-12 review:
  * the quote's words must appear in order; a loose (fuzzy) match never
    supports more than medium confidence, and only an exact match may close or
    change an existing loop (the workflow enforces that with `how`);
  * a negation the transcript carries and the quote drops ("No, we will NOT
    sign") means the quote is not supported at all;
  * the owner check uses the turns the quote actually matched, not merely the
    turns the agent cited, and the cited turns are searched before their neighbours;
  * a quote that adds a negation the transcript lacks is not supported either;
  * a negation only counts on the turn the quote starts on, so the other speaker's
    "I hope not" cannot negate the commitment that follows;
  * Hindi in Devanagari keeps its vowel signs when normalised;
  * garbled or bleed-flagged turns cap confidence at medium.
"""
import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from ..schemas.common import conf_min

_APOS = re.compile(r"[’']")
_SPACE = re.compile(r"\s+")
NEGATIONS = {
    "not", "no", "never", "nahi", "nahin", "mat", "dont", "doesnt", "didnt", "wont", "cant", "cannot",
    "shouldnt", "wouldnt", "isnt", "arent", "wasnt", "werent", "havent", "hasnt", "hadnt", "neither", "nor",
    "नहीं", "नही", "मत",          # Whisper writes Hindi in Devanagari
}
NEGATION_LOOKBACK = 2       # tokens before a match that can still negate it ("will not | sign ...")


def _strip_punct(text: str) -> str:
    """Punctuation and symbols become spaces; letters, digits and combining marks stay.

    A regex on \\w would drop Devanagari vowel signs (category Mn/Mc), turning every Hindi word into a
    consonant skeleton and making unrelated words match each other."""
    return "".join(ch if (ch.isspace() or unicodedata.category(ch)[0] in "LMN") else " " for ch in text)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").lower()
    return _SPACE.sub(" ", _strip_punct(_APOS.sub("", text))).strip()


@dataclass
class EvidenceCheck:
    found: bool
    how: str                       # exact | fuzzy | negated | missing | no_quote | no_turns | unknown_turn
    garbled: bool = False
    bleed: bool = False
    channels: set = field(default_factory=set)      # channels of the turns the quote matched
    notes: list = field(default_factory=list)


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def _find(hay: list, needle: list):
    n = len(needle)
    for i in range(len(hay) - n + 1):
        if hay[i:i + n] == needle:
            return i
    return None


def _negated(hay: list, origin: list, start: int, end: int, needle: list) -> bool:
    """The transcript negates the quote: a negation just before or inside the match, on the same turn.

    Only the turn the match starts on counts, so "I hope not." from the other speaker cannot negate the
    commitment that follows it."""
    if any(t in NEGATIONS for t in needle):
        return False
    turn = origin[start]
    return any(hay[k] in NEGATIONS for k in range(max(0, start - NEGATION_LOOKBACK), end) if origin[k] == turn)


def _hay(window, turns_by_idx):
    hay, origin = [], []
    for j in window:
        tokens = normalize(_get(turns_by_idx[j], "text") or "").split()
        hay += tokens
        origin += [j] * len(tokens)
    return hay, origin


def _match(hay: list, needle: list):
    """(start, end, how, note): the quote's position in hay, or a note saying why there is none."""
    start = _find(hay, needle)
    if start is not None:
        return start, start + len(needle), "exact", None
    blocks = [b for b in difflib.SequenceMatcher(None, needle, hay, autojunk=False).get_matching_blocks()
              if b.size]
    matched = sum(b.size for b in blocks)
    if len(needle) < 3 or not blocks or matched / len(needle) < 0.85:
        return None, None, None, "quote not found in cited turns"
    start, end = blocks[0].b, blocks[-1].b + blocks[-1].size
    if end - start > len(needle) * 1.5 + 3:
        return None, None, None, "quote's words are scattered, not a quotation"
    # A loose match may drop one quote word. It must never be a negation: "we will NOT sign" is not
    # supported by a transcript that says "we will sign".
    matched_needle = {k for b in blocks for k in range(b.a, b.a + b.size)}
    if any(needle[k] in NEGATIONS and k not in matched_needle for k in range(len(needle))):
        return None, None, "negated", "the quote negates what the transcript says"
    return start, end, "fuzzy", None


def check(quote: str, turn_idxs, turns_by_idx: dict) -> EvidenceCheck:
    """turns_by_idx maps idx -> row/dict with text, channel, quality, bleed_flag."""
    idxs = list(turn_idxs or [])
    if not idxs:
        return EvidenceCheck(False, "no_turns", notes=["no turns cited"])
    unknown = [i for i in idxs if i not in turns_by_idx]
    if unknown:
        return EvidenceCheck(False, "unknown_turn", notes=[f"cited turns do not exist: {unknown}"])
    cited = [turns_by_idx[i] for i in idxs]
    result = EvidenceCheck(False, "missing",
                           garbled=any(_get(t, "quality") == "garbled" for t in cited),
                           bleed=any(_get(t, "bleed_flag") for t in cited))
    needle = normalize(quote).split()
    if not needle:
        result.how = "no_quote"
        result.notes.append("no quote given")
        return result
    # The cited turns are searched first; only when the quote is not there are the neighbours added,
    # because quotes may straddle a turn boundary. Searching neighbours first would let the same words on
    # the other speaker's previous turn claim the match and flip the owner check.
    cited_window = sorted(set(idxs))
    wide_window = sorted({j for i in idxs for j in (i - 1, i, i + 1) if j in turns_by_idx})
    windows = [cited_window] + ([wide_window] if wide_window != cited_window else [])
    start = None
    for window in windows:
        hay, origin = _hay(window, turns_by_idx)
        start, end, how, note = _match(hay, needle)
        if start is not None:
            break
    if start is None:
        if how == "negated":
            result.how = "negated"
        result.notes.append(note)
        return result
    if _negated(hay, origin, start, end, needle):
        result.how = "negated"
        result.notes.append("the transcript negates what the quote asserts")
        return result
    matched_turns = {origin[k] for k in range(start, end)}
    result.found, result.how = True, how
    result.channels = {_get(turns_by_idx[j], "channel") for j in matched_turns}
    result.garbled = result.garbled or any(_get(turns_by_idx[j], "quality") == "garbled" for j in matched_turns)
    result.bleed = result.bleed or any(_get(turns_by_idx[j], "bleed_flag") for j in matched_turns)
    return result


OWNER_CHANNEL = {"me": "me", "prospect": "them"}


def judge(confidence: str, quote: str, turn_idxs, turns_by_idx: dict, owner: str | None = None,
          requires_quote: bool = True, source: str | None = None) -> tuple[str, list[str], EvidenceCheck]:
    """Return (validated confidence, notes, check). Never raises confidence."""
    result = check(quote, turn_idxs, turns_by_idx)
    notes = list(result.notes)
    new_conf = confidence
    if (requires_quote and not result.found) or result.how == "negated":
        new_conf = "low"
    if result.how == "fuzzy":
        new_conf = conf_min(new_conf, "medium")
        notes.append("loose match only; the exact words were not found")
    if result.garbled:
        new_conf = conf_min(new_conf, "medium")
        notes.append("cites a garbled turn")
    if result.bleed:
        new_conf = conf_min(new_conf, "medium")
        notes.append("cites a turn that may be the other side's audio leaking into the mic")
    expected = OWNER_CHANNEL.get(owner or "")
    if expected and result.found and expected not in result.channels:
        if owner == "me" and source == "explicit_commitment":
            # Only the seller's own words can be the seller's explicit commitment. Text on the other channel that
            # says he committed (a buyer's summary, or an instruction planted in a transcript) is not.
            new_conf = "low"
            notes.append("claims the seller committed, but the quoted words are not on the seller's channel")
        else:
            new_conf = conf_min(new_conf, "medium")
            notes.append(f"owner is {owner} but the quoted words are not on the {expected} channel")
    return new_conf, notes, result
