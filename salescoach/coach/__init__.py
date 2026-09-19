"""Phase 2: real-time live coaching.

During a call the seller gets at most ONE short, actionable nudge on screen, and
only when it really matters. The pieces, in the order a transcript segment
flows through them:

  state.py      what the coach knows about this conversation right now
  detectors.py  FAST path: deterministic English + Hinglish rules, < 50 ms
  slow_pass.py  SLOW path: a Claude pass every ~60 s over the last 8 minutes
  ranker.py     the intervention budget: one visible nudge, cooldown, per-call cap
  engine.py     LiveCoach: wires the above to the live hub and the nudges table
  replay.py     runs a finished call through the same engine, for tuning

Every threshold is in config/live_coach.yaml (see settings.py).
"""
