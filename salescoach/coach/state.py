"""Conversation state for one live call: what the coach knows at this moment.

Every trigger is really a question about the conversation so far, not about
the last sentence. "Quantify the impact" only makes sense if a problem is on
the table and no number has followed; "Ask who else is involved" only if the
person named is not already a known stakeholder. So the detectors read this
state, and both the fast rules and the slow Claude pass write to it.

Slots only move forward, unknown -> asked -> partial -> known. The fast path
can only say "the seller asked about it" or "a number was mentioned"; the slow
pass is the one that can say "known". A weaker update never downgrades a
stronger one (the memory-gate rule, applied inside a single call). Each slot
counts ME's asks about it; a pending nudge is "resolved" when ME asks again
after it was raised, or the slot's status rises.
"""
from dataclasses import asdict, dataclass

from .text import INTERROGATIVE, mmss, norm

SLOTS = ("pain", "impact", "root_cause", "status_quo_cost", "decision_process", "economic_buyer",
         "stakeholders", "next_step", "objections", "buying_signals")
RANK = {"unknown": 0, "asked": 1, "partial": 2, "known": 3}
PHASES = ("opening", "discovery", "pitch", "negotiation", "close")


@dataclass
class Seg:
    idx: int
    channel: str                 # me | them
    t_start: float
    t_end: float
    text: str
    norm: str = ""
    clean: str = ""              # norm with neutralising phrases blanked
    n_words: int = 0


@dataclass
class Slot:
    status: str = "unknown"
    value: str = ""
    source: str = ""
    t: float | None = None
    asks: int = 0                # how many times ME asked about it


class ConversationState:
    def __init__(self, cfg: dict, vocab, known_people=()):
        self.cfg, self.vocab = cfg, vocab
        self.segments: list[Seg] = []
        self.slots = {s: Slot() for s in SLOTS}
        self.known_people = {norm(p) for p in known_people if p and norm(p)}
        self.stakeholders: dict[str, dict] = {}       # key -> {display, t, known}
        self.objections: list[dict] = []
        self.buying_signals: list[dict] = []
        self.me_s = self.them_s = 0.0
        self.me_questions = 0
        self.last_speaker = None
        self.t_now = 0.0
        self.phase, self.phase_source, self.phase_t = "opening", "fast", 0.0
        self.reason_to_change = False
        # detector bookkeeping, kept here so a snapshot shows it
        self.pending_problem: Seg | None = None
        self.pending_important: Seg | None = None
        self.last_emit: dict[str, float] = {}

    # ---- updates ------------------------------------------------------------

    def prepare(self, seg: Seg) -> Seg:
        seg.norm = norm(seg.text)
        seg.clean = self.vocab.neutralise(seg.norm)
        seg.n_words = len(seg.norm.split())
        return seg

    def add(self, seg: Seg) -> None:
        self.prepare(seg)
        self.segments.append(seg)
        self.t_now = max(self.t_now, seg.t_end)
        duration = max(0.0, seg.t_end - seg.t_start)
        if seg.channel == "me":
            self.me_s += duration
        else:
            self.them_s += duration
        self.last_speaker = seg.channel
        t, lex = seg.t_end, self.vocab.lex

        number = lex["quantity"].first(seg.norm)
        problem_here = lex["problem"].any(seg.clean)
        if number and (problem_here or self.slots["pain"].status != "unknown"):
            self.mark("impact", "partial", f"number mentioned: {number}", "fast", t)

        if seg.channel == "me":
            question = self.is_question(seg)
            if question:
                self.me_questions += 1
            for slot, hit in self._asks(seg, question):
                self.mark(slot, "asked", f"ME asked: {hit}", "fast", t)
            if question and any(key in seg.norm for key in self.stakeholders):
                self.mark("stakeholders", "asked", "ME asked about a named stakeholder", "fast", t)
        else:
            if problem_here and lex["business"].any(seg.clean):
                self.mark("pain", "partial", seg.text[:160], "fast", t)
            urgency = lex["urgency"].first(seg.clean)
            if urgency:
                self.reason_to_change = True
                self.mark("status_quo_cost", "partial", f"reason to change: {urgency}", "fast", t)
            date = self.vocab.asks["next_step"].first(seg.norm) if "next_step" in self.vocab.asks else None
            if date:
                self.mark("next_step", "partial", f"buyer named a time: {date}", "fast", t)
        self._update_phase(t)

    def _asks(self, seg: Seg, question: bool):
        for slot, cues in self.vocab.asks.items():
            hits = cues.find(seg.norm)
            if not hits:
                continue
            if slot == "next_step" and not question:
                yield slot, hits[0]           # ME naming a date makes the next step specific too
                continue
            if question or any(w in INTERROGATIVE for h in hits for w in h.split()):
                yield slot, hits[0]

    def is_question(self, seg: Seg) -> bool:
        return "?" in seg.text or self.vocab.lex["question"].any(seg.norm)

    def mark(self, slot: str, status: str, value: str = "", source: str = "fast", t=None) -> bool:
        cur = self.slots[slot]
        t = self.t_now if t is None else t
        if status == "asked" and source == "fast":
            cur.asks += 1                     # a fresh ask counts as acting on a pending nudge
        if RANK[status] < RANK[cur.status]:
            return False
        if RANK[status] == RANK[cur.status]:
            if status == "asked":
                cur.t, cur.value = t, (value or cur.value)[:200]
            elif source == "slow" and value and value != cur.value:
                cur.value, cur.source = value[:200], source
            return False
        cur.status, cur.value, cur.source, cur.t = status, (value or cur.value)[:200], source, t
        return True

    def set_phase(self, phase: str, source: str, t: float) -> None:
        if phase in PHASES:
            self.phase, self.phase_source, self.phase_t = phase, source, t

    def _update_phase(self, now: float) -> None:
        p = self.cfg["phase"]
        if self.phase_source == "slow" and now - self.phase_t < p["slow_hold_s"]:
            return
        if now < p["opening_s"]:
            self.set_phase("opening", "fast", now)
            return
        recent = []
        for s in reversed(self.segments):
            if s.t_end < now - p["window_s"]:
                break
            recent.append(s)
        lex = self.vocab.lex

        def hits(name):
            return sum(len(lex[name].find(s.norm)) for s in recent)

        me = sum(max(0.0, s.t_end - s.t_start) for s in recent if s.channel == "me")
        total = sum(max(0.0, s.t_end - s.t_start) for s in recent) or 1.0
        if hits("close") >= p["min_cue_hits"]:
            phase = "close"
        elif hits("price") >= p["min_cue_hits"]:
            phase = "negotiation"
        elif me / total >= p["pitch_me_share"] and hits("pitch") >= 1:
            phase = "pitch"
        else:
            phase = "discovery"
        self.set_phase(phase, "fast", now)

    def note_stakeholder(self, key: str, display: str, t: float) -> tuple[bool, bool]:
        """Record a named role/person. Returns (is_new, is_known)."""
        known = self.is_known_person(key)
        if key in self.stakeholders:
            return False, self.stakeholders[key]["known"]
        self.stakeholders[key] = {"display": display, "t": t, "known": known}
        return True, known

    def is_known_person(self, key: str) -> bool:
        key = norm(key)
        if not key:
            return False
        for person in self.known_people:
            if key == person or key in person.split() or (len(key) > 3 and key in person):
                return True
        return False

    # ---- reads --------------------------------------------------------------

    @property
    def me_share(self) -> float | None:
        total = self.me_s + self.them_s
        return round(self.me_s / total, 3) if total else None

    def transcript(self, now: float, window_s: float) -> list[str]:
        lines = []
        for s in self.segments:
            if s.t_end >= now - window_s:
                text = s.text.replace("<", "(").replace(">", ")")
                lines.append(f"[{mmss(s.t_start)}] {'ME' if s.channel == 'me' else 'THEM'}: {text}")
        return lines

    def snapshot(self) -> dict:
        return {
            "t": round(self.t_now, 1),
            "phase": self.phase, "phase_source": self.phase_source,
            "me_share": self.me_share, "me_questions": self.me_questions, "last_speaker": self.last_speaker,
            "segments": len(self.segments),
            "slots": {k: asdict(s) for k, s in self.slots.items()},
            "stakeholders_named": [{"name": v["display"], "known": v["known"], "t": round(v["t"], 1)}
                                   for v in self.stakeholders.values()],
            "open_objections": self.objections[-5:],
            "buying_signals": self.buying_signals[-5:],
            "reason_to_change": self.reason_to_change,
        }

    def brief(self) -> dict:
        """The compact form the slow pass reads: status and value per slot."""
        return {
            "phase": self.phase,
            "known": {k: f"{s.status}: {s.value}" if s.value else s.status for k, s in self.slots.items()},
            "stakeholders_named": [f"{v['display']} ({'known' if v['known'] else 'not on record'})"
                                   for v in self.stakeholders.values()],
            "me_talk_share": self.me_share, "me_questions": self.me_questions,
        }
