"""The intervention ranker: which candidate, if any, reaches the screen.

This is where the over-coaching guard lives (plan, failure-mode table). A
nudge costs the seller attention in the middle of a sentence, so the default
answer is "not now":

  strength = trigger weight x confidence x recency x phase fit
  score    = strength x novelty

and a hard budget on top, all from config/live_coach.yaml: one visible nudge
at a time, a cooldown after each, a per-call cap, a minimum score, no repeat
of a trigger within five minutes unless clearly stronger, moment triggers
stale after 25 s, and nothing in the first minute except an objection.

"Clearly stronger" compares strength, not score: novelty already discounts a
repeat, and counting that discount twice would turn a 25% margin into ~80%.

Candidates wait in a small pool (one per trigger), because a state trigger
("Ask who else is involved") blocked by the cooldown can still be the right
nudge a minute later. A blocked candidate stays in the pool until the block
lifts or it goes stale, even if its score decays below the minimum meanwhile:
the phase can move (a vague "let's see" matters more at the close), and the
block, not the decay, is the honest answer to "why wasn't this shown?".

Blocks that can lift: warmup, visible, cooldown, repeat, outranked. Terminal:
stale, resolved, below_min_score, max_per_call, duplicate. A retired
candidate's suppressed_reason is the block that held it back while it was
good enough to show, or the terminal reason if nothing did.
"""
from .detectors import MAX_WORDS, Candidate
from .state import RANK

TEMPORARY = ("warmup", "visible", "cooldown", "repeat", "outranked")


class Ranker:
    def __init__(self, cfg: dict):
        b, s = cfg["budget"], cfg["scoring"]
        self.max_visible = int(b.get("max_visible", 1))
        self.display_s = float(b["display_s"])
        self.cooldown_s = float(b["cooldown_s"])
        # Replay of a real call (2 Sep): a real price objection 46 s after a weaker nudge was lost
        # to the cooldown. An objection is the moment "don't answer yet" matters most, so it waits less.
        self.objection_min_gap_s = float(b.get("objection_min_gap_s", b["cooldown_s"]))
        self.max_per_call = int(b["max_per_call"])
        self.min_score = float(b["min_score"])
        self.repeat_window_s = float(b["repeat_window_s"])
        self.repeat_margin = float(b["repeat_margin"])
        self.moment_stale_s = float(b["moment_stale_s"])
        self.state_stale_s = float(b["state_stale_s"])
        self.warmup_s = float(b["warmup_s"])
        self.warmup_exempt = set(b.get("warmup_exempt") or [])
        self.weights = s["weights"]
        self.novelty = s["novelty"]
        self.recency_floor = float(s.get("recency_floor", 0.5))
        self.phase_fit = s.get("phase_fit") or {}
        self.pending: dict[str, Candidate] = {}
        self.shown: list[Candidate] = []
        self.visible: list[Candidate] = []

    # ---- scoring --------------------------------------------------------------

    def stale_after(self, c: Candidate) -> float:
        return self.moment_stale_s if c.kind == "moment" else self.state_stale_s

    def strength(self, c: Candidate, now: float, phase: str) -> float:
        """How strong the signal is right now, before any repeat discount."""
        age = max(0.0, now - c.created_at)
        stale = self.stale_after(c)
        if age > stale:
            return 0.0
        recency = 1.0 - (1.0 - self.recency_floor) * (age / stale)
        fit = (self.phase_fit.get(phase) or {}).get(c.trigger, 1.0)
        return round(float(self.weights.get(c.trigger, 0.5)) * c.confidence * recency * fit, 4)

    def novelty_of(self, c: Candidate) -> float:
        if any(p.trigger == c.trigger and c.entity and p.entity == c.entity for p in self.shown):
            return float(self.novelty["repeat_entity"])
        if any(p.trigger == c.trigger for p in self.shown):
            return float(self.novelty["repeat_trigger"])
        return float(self.novelty["first"])

    def score(self, c: Candidate, now: float, phase: str) -> float:
        return round(self.strength(c, now, phase) * self.novelty_of(c), 4)

    def _rescore(self, c: Candidate, now: float, state) -> None:
        phase = state.phase if state is not None else "discovery"
        c.strength = self.strength(c, now, phase)
        c.score = round(c.strength * self.novelty_of(c), 4)

    # ---- the pool -------------------------------------------------------------

    def offer(self, c: Candidate) -> list[Candidate]:
        """Add a candidate; returns any it retired (itself or a weaker duplicate)."""
        if len(c.text.split()) > MAX_WORDS:
            c.suppressed_reason = "too_long"
            return [c]
        cur = self.pending.get(c.trigger)
        if cur is None:
            self.pending[c.trigger] = c
            return []
        if c.confidence >= cur.confidence:
            self.pending[c.trigger] = c
            cur.suppressed_reason = (cur.last_block if cur.held else None) or "duplicate"
            return [cur]
        c.suppressed_reason = "duplicate"
        return [c]

    def _held_or(self, c: Candidate, reason: str) -> str:
        return (c.last_block if c.held else None) or reason

    def _terminal(self, c: Candidate, now: float, state) -> str | None:
        """Reasons that retire a candidate whatever blocks it."""
        if len(self.shown) >= self.max_per_call:
            return "max_per_call"
        if c.kind == "state" and c.slot and state is not None:
            slot = state.slots[c.slot]
            if slot.asks > c.slot_asks or RANK[slot.status] > c.slot_rank:
                return "resolved"
        if now - c.created_at > self.stale_after(c):
            return self._held_or(c, "stale")
        return None

    def _blocked(self, c: Candidate, now: float) -> str | None:
        if now < self.warmup_s and c.trigger not in self.warmup_exempt:
            return "warmup"
        if len(self.visible) >= self.max_visible:
            return "visible"
        if self.shown:
            limit = self.objection_min_gap_s if c.trigger == "objection" else self.cooldown_s
            if now - self.shown[-1].shown_at < limit:
                return "cooldown"
        prev = next((p for p in reversed(self.shown) if p.trigger == c.trigger), None)
        if prev is not None and now - prev.shown_at < self.repeat_window_s \
                and c.strength < prev.strength * (1.0 + self.repeat_margin):
            return "repeat"
        return None

    def select(self, now: float, state=None) -> tuple[Candidate | None, list[Candidate]]:
        """Pick at most one candidate to show now. Returns (winner, retired)."""
        self.visible = [v for v in self.visible if not v.dismissed and now < v.shown_at + self.display_s]
        retired, eligible = [], []
        for trigger, c in list(self.pending.items()):
            reason = self._terminal(c, now, state)
            if reason is None:
                self._rescore(c, now, state)
                block = self._blocked(c, now)
                if block:
                    c.last_block = block
                    c.held = c.held or c.score >= self.min_score
                    continue
                if c.score < self.min_score:
                    reason = self._held_or(c, "below_min_score")
            if reason:
                del self.pending[trigger]
                c.suppressed_reason = reason
                retired.append(c)
            else:
                eligible.append(c)
        if not eligible:
            return None, retired
        eligible.sort(key=lambda c: (c.score, c.confidence, c.created_at), reverse=True)
        winner = eligible[0]
        for other in eligible[1:]:
            other.last_block, other.held = "outranked", True
        del self.pending[winner.trigger]
        winner.shown, winner.shown_at = True, now
        self.shown.append(winner)
        self.visible.append(winner)
        return winner, retired

    def unshow(self, c) -> None:
        """The engine could not persist a winner: undo select()'s bookkeeping so nothing was spent."""
        c.shown, c.shown_at, c.shown_wall = False, None, None
        for lst in (self.shown, self.visible):
            if c in lst:
                lst.remove(c)
        self.pending[c.trigger] = c

    def dismiss(self, nudge_id: int) -> bool:
        for c in self.shown:
            if c.id == nudge_id:
                c.dismissed = True
                self.visible = [v for v in self.visible if v is not c]
                return True
        return False

    def flush(self, reason: str = "call_ended") -> list[Candidate]:
        retired = []
        for c in self.pending.values():
            c.suppressed_reason = self._held_or(c, reason)
            retired.append(c)
        self.pending.clear()
        return retired

    def current(self, now: float) -> Candidate | None:
        live = [v for v in self.visible if not v.dismissed and now < v.shown_at + self.display_s]
        return live[-1] if live else None
