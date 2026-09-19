"""email_voice: at most ONE candidate style rule per sent edit, from the
draft-vs-final difference, by deterministic heuristics only.

A rule is a key plus a fixed human sentence. Nothing from the email itself is
ever stored: the only text a rule can carry is a filler phrase or a sign-off
word from the FIXED lists in config/learning.yaml, never a sentence, a name or
a number from the draft or the final (either may quote a customer).

Order, most specific first; the first heuristic that fires is the edit's rule:
  remove_phrase:<id>   a listed filler phrase is in the draft and gone from the final
  signoff:<word>       the sign-off changed to another listed sign-off
  greeting:<word> / drop_greeting
  drop_closing_line    a stock closing line is in the draft and no stock closing is in the final
  no_exclamation       the draft had exclamation marks, the final has none
  shorten / lengthen   final/draft word ratio past the configured thresholds
"""
import re

from . import cfg

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?")
_GREETING = re.compile(r"^(hi|hello|hey|dear|good (?:morning|afternoon|evening)|namaste)\b", re.I)

SENTENCES = {
    "drop_greeting": "Skip the greeting line and start with the point.",
    "drop_closing_line": "Drop the stock closing line (offers to answer questions, looking forward to hearing back).",
    "no_exclamation": "No exclamation marks.",
    "shorten": "Keep drafts shorter; you cut them down before sending.",
    "lengthen": "Drafts are too thin; you add detail before sending.",
}


# The same rules as instructions to a drafter (phase F2). SENTENCES speak to the seller on the Learning
# page ("you cut them down"); a prompt needs an imperative the model cannot misread as being about itself.
IMPERATIVES = {
    "drop_greeting": "Skip the greeting line and start with the point.",
    "drop_closing_line": "Do not end with a stock closing line (an offer to answer questions, looking forward to "
                         "hearing back).",
    "no_exclamation": "Use no exclamation marks.",
    "shorten": "Keep it shorter than the draft would be.",
    "lengthen": "Give more detail than the draft would.",
}


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("’", "'").split())


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _signoff(text: str, signoffs) -> str | None:
    known = {str(s).lower() for s in signoffs}
    for line in reversed(_lines(text)[-6:]):
        candidate = line.lower().rstrip(",.!- ").strip()
        if candidate in known:
            return candidate
    return None


def _greeting(text: str) -> str | None:
    lines = _lines(text)
    m = _GREETING.match(lines[0]) if lines else None
    return m.group(1).lower() if m else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def derive_rule(draft: str, final: str, settings: dict | None = None) -> dict | None:
    """The one rule this edit supports, or None. Returns {"key", "evidence"}; evidence holds counts only."""
    s = settings or cfg("email_voice")
    d, f = _norm(draft), _norm(final)
    if not d or not f or d == f:
        return None
    dw, fw = word_count(draft), word_count(final)
    evidence = {"draft_words": dw, "final_words": fw}

    for pid, phrase in (s.get("filler_phrases") or {}).items():
        p = _norm(str(phrase))
        if p and re.search(rf"(?<!\w){re.escape(p)}(?!\w)", d) and not re.search(rf"(?<!\w){re.escape(p)}(?!\w)", f):
            return {"key": f"remove_phrase:{pid}", "evidence": evidence}

    so_d, so_f = _signoff(draft, s.get("signoffs") or ()), _signoff(final, s.get("signoffs") or ())
    if so_d and so_f and so_d != so_f:
        return {"key": f"signoff:{_slug(so_f)}", "evidence": evidence}

    g_d, g_f = _greeting(draft), _greeting(final)
    if g_d and g_f and g_d != g_f:
        return {"key": f"greeting:{_slug(g_f)}", "evidence": evidence}
    if g_d and not g_f:
        return {"key": "drop_greeting", "evidence": evidence}

    closings = [_norm(str(c)) for c in (s.get("closing_lines") or [])]
    if any(c and c in d for c in closings) and not any(c and c in f for c in closings):
        return {"key": "drop_closing_line", "evidence": evidence}

    if "!" in (draft or "") and "!" not in (final or ""):
        return {"key": "no_exclamation", "evidence": {**evidence, "draft_exclamations": draft.count("!")}}

    if dw >= int(s.get("min_draft_words", 25)):
        ratio = fw / dw
        if ratio <= float(s.get("shorten_ratio", 0.8)):
            return {"key": "shorten", "evidence": {**evidence, "ratio": round(ratio, 2)}}
        if ratio >= float(s.get("lengthen_ratio", 1.25)):
            return {"key": "lengthen", "evidence": {**evidence, "ratio": round(ratio, 2)}}
    return None


def sentence(key: str, settings: dict | None = None) -> str:
    """The human sentence of a rule. Built from fixed templates and the config lists only."""
    s = settings or cfg("email_voice")
    if key in SENTENCES:
        return SENTENCES[key]
    kind, _, arg = key.partition(":")
    if kind == "remove_phrase":
        phrase = (s.get("filler_phrases") or {}).get(arg)
        return f'Do not write "{phrase}".' if phrase else "Drop a stock filler phrase."
    if kind == "signoff":
        word = next((w for w in (s.get("signoffs") or []) if _slug(str(w)) == arg), arg.replace("_", " "))
        return f'Sign off with "{str(word).capitalize()}".'
    if kind == "greeting":
        return f'Open with "{arg.replace("_", " ").capitalize()}".'
    return key.replace("_", " ").capitalize() + "."


def imperative(key: str, settings: dict | None = None) -> str:
    """The rule as ONE imperative sentence for a drafter's prompt. Same sources as sentence(): fixed
    templates and the config lists, so nothing a customer wrote can be in it."""
    return IMPERATIVES.get(key) or sentence(key, settings)
