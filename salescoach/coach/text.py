"""Text helpers for the fast path: normalisation and cue matching.

The fast path has a 50 ms budget per segment and runs on every line, so each
cue list is compiled once into a single alternation and matched on
lower-cased text. Word boundaries matter more than usual here: Hinglish is
full of short words ("do", "char", "md") that are also substrings of
unrelated English.

Neutralising phrases ("no problem", "no doubt", "on board") are blanked out
before problem / objection / role cues run, because in Indian business
English they usually mean the opposite of the cue inside them.
"""
import re
import unicodedata

_KEEP = set("%?₹.'-")
_WS = re.compile(r"\s+")


def _tokens(text: str) -> list[str]:
    """Words in any script. Category-based because \\w excludes combining marks, which would split a
    Devanagari word at every vowel sign."""
    out, cur = [], []
    for ch in text:
        if unicodedata.category(ch)[0] in "LMN":
            cur.append(ch)
        elif cur:
            out.append("".join(cur)); cur = []
    if cur:
        out.append("".join(cur))
    return out


def _strip_punct(text: str) -> str:
    # Category-based, not \w-based: Devanagari vowel signs are combining marks, which \w would drop.
    return "".join(ch if (ch.isspace() or ch in _KEEP or unicodedata.category(ch)[0] in "LMN") else " "
                   for ch in text)
INTERROGATIVE = frozenset({"how", "what", "who", "why", "when", "which", "kitna", "kitne", "kitni", "kaun",
                           "kab", "kyun", "kyon", "kaise", "kya", "kisko", "kise"})


def norm(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "").lower().replace("’", "'").replace("‘", "'")
    t = _strip_punct(t)
    return _WS.sub(" ", t).strip()


def word_count(text: str) -> int:
    return len((text or "").split())


def mmss(seconds) -> str:
    if seconds is None:
        return "--:--"
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def _pattern(entry: str) -> str:
    entry = str(entry)
    if entry.startswith("re:"):
        return entry[3:]
    words = entry.lower().split()
    return r"(?<![\w'])" + r"\s+".join(re.escape(w) for w in words) + r"(?![\w'])"


class Cues:
    """One compiled cue list. Entries prefixed "re:" are raw regexes."""

    def __init__(self, entries=None):
        self.entries = [str(e) for e in (entries or []) if isinstance(e, str) and e.strip()]
        self.rx = re.compile("|".join(f"(?:{_pattern(e)})" for e in self.entries)) if self.entries else None

    def find(self, text: str) -> list[str]:
        return [m.group(0).strip() for m in self.rx.finditer(text)] if self.rx else []

    def first(self, text: str):
        m = self.rx.search(text) if self.rx else None
        return m.group(0).strip() if m else None

    def any(self, text: str) -> bool:
        return bool(self.rx and self.rx.search(text))


def content_words(text_norm: str, stop: set) -> set:
    return {w for w in _tokens(text_norm) if len(w) >= 3 and w not in stop}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Vocab:
    """Every cue list from the config, compiled once per coach run."""

    def __init__(self, cfg: dict):
        lex = cfg.get("lexicon") or {}
        self.stop = {str(w).lower() for w in lex.get("stopwords") or [] if isinstance(w, str)}
        self.lex = {k: Cues(v) for k, v in lex.items() if k not in ("stopwords", "neutralisers")}
        self.neutral = Cues(lex.get("neutralisers"))
        self.asks = {k: Cues(v) for k, v in (cfg.get("asks") or {}).items()}
        triggers = cfg.get("triggers") or {}
        self.trig = {name: {cat: Cues(v) for cat, v in (t.get("cues") or {}).items()}
                     for name, t in triggers.items()}
        sg = triggers.get("stakeholder_gap") or {}
        self.roles = [(re.compile(_pattern(k)), str(v)) for k, v in (sg.get("roles") or {}).items()]
        self.names = [re.compile(str(n)[3:] if str(n).startswith("re:") else re.escape(str(n)))
                      for n in sg.get("names") or []]
        self.not_names = {str(n).lower() for n in sg.get("not_names") or []}
        self.decision_words = Cues(sg.get("decision_words"))
        self.commit_nouns = Cues((triggers.get("weak_commitment") or {}).get("commit_nouns"))

    def neutralise(self, text_norm: str) -> str:
        return self.neutral.rx.sub(" ", text_norm) if self.neutral.rx else text_norm

    def find_roles(self, text_norm: str) -> list[tuple[str, str]]:
        """(key, display) for each role cue in the text; a role inside a longer
        matched role ("director" inside "managing director") is dropped."""
        found = []
        for rx, display in self.roles:
            for m in rx.finditer(text_norm):
                key = _WS.sub(" ", m.group(0).strip())
                found.append((key, display.replace("{match}", key), m.span()))
        out = []
        for key, display, span in found:
            if any(o[2] != span and o[2][0] <= span[0] and span[1] <= o[2][1] for o in found):
                continue
            if all(key != k for k, _ in out):
                out.append((key, display))
        return out

    def find_names(self, text_norm: str) -> list[str]:
        out = []
        for rx in self.names:
            for m in rx.finditer(text_norm):
                name = (m.group(1) if m.groups() else m.group(0)).strip()
                if not name or name in self.not_names or name in self.stop or name in out:
                    continue
                if m.groups() and m.start(1) == 0:
                    continue        # "Hello ji", "Namaste ji": an utterance-opening particle, not a person
                out.append(name)
        return out
