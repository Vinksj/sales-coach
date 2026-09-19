"""Replay a finished call through the live coach, for demos and tuning.

Capture permission is not sorted yet, and even when it is, tuning thresholds
on live calls would mean living with a noisy coach for weeks. So the coach is
tested on calls already in sales.db: their final turns are fed into the SAME
engine as synthetic live segments, through a private in-memory hub, on a
virtual clock moved forward at a speed factor.

Timing: turns with t_start/t_end keep them. Text-only calls (Granola
imports, pastes) have none, so each turn gets ~150 words per minute plus a
short gap; `duration_s` stretches that to a known call length, because a
garbled transcript drops words and would otherwise replay too fast.

The virtual clock also advances in one-second steps between lines, so
cooldowns expire and state nudges can surface in the silences exactly as
they would live. The slow pass runs synchronously with the clock frozen,
and its result lands at snapshot + real latency: a replay sees the same
staleness a live call would.
"""
import time
from typing import Callable, Optional

from .. import repo
from ..live.hub import Hub
from ..store import stores
from .engine import LiveCoach, ReplayClock, call_topic

WPM = 150
GAP_S = 0.4
MIN_TURN_S = 1.2


def plan(turns, duration_s: Optional[float] = None, wpm: float = WPM) -> list[dict]:
    """Synthetic live segments with call-relative timing."""
    turns = [dict(t) for t in turns if (t["text"] or "").strip()]
    timed = turns and all(t.get("t_start") is not None and t.get("t_end") is not None for t in turns)
    out = []
    if timed:
        for t in turns:
            out.append({"idx": t["idx"], "channel": t["channel"], "t_start": float(t["t_start"]),
                        "t_end": float(t["t_end"]), "text": t["text"]})
        return out
    clock = 0.0
    for t in turns:
        dur = max(MIN_TURN_S, len(t["text"].split()) * 60.0 / wpm)
        out.append({"idx": t["idx"], "channel": t["channel"], "t_start": clock, "t_end": clock + dur,
                    "text": t["text"]})
        clock += dur + GAP_S
    if duration_s and out and out[-1]["t_end"] > 0:
        scale = duration_s / out[-1]["t_end"]
        for s in out:
            s["t_start"], s["t_end"] = round(s["t_start"] * scale, 2), round(s["t_end"] * scale, 2)
    return out


def replay(call_id: str, db_path=None, speed: float = 20.0, slow: bool = True, duration_s: Optional[float] = None,
           overrides: Optional[dict] = None, provider=None, publish_hub=None, publish_current: bool = False,
           session: Optional[str] = None, stop=None, on_engine: Optional[Callable] = None,
           sleep: Callable = time.sleep) -> dict:
    conn = stores.sales(db_path)
    try:
        if repo.get_call(conn, call_id) is None:
            raise KeyError(call_id)
        turns = repo.turns(conn, call_id, "final") or repo.turns(conn, call_id, "live")
    finally:
        conn.close()
    if not turns:
        raise ValueError(f"{call_id} has no transcript to replay")
    segments = plan(turns, duration_s)
    hub_in, clock = Hub(maxsize=100000), ReplayClock()
    engine = LiveCoach(call_id, db_path, hub=hub_in, provider=provider, overrides=overrides,
                       publish_hub=publish_hub or Hub(), clock=clock, mode="replay", slow=slow, slow_mode="sync",
                       emit_events=False, publish_current=publish_current, session=session)
    engine.attach(threaded=False, backfill=False)
    if on_engine:
        on_engine(engine)
    topic = call_topic(call_id)

    def wait(dt):
        if speed and 0 < speed < 1000 and dt > 0:
            sleep(dt / speed)

    for seg in segments:
        if stop is not None and stop.is_set():
            break
        while clock.t + 1.0 < seg["t_end"]:
            wait(1.0)
            clock.advance(clock.t + 1.0)
            engine.tick()
        wait(seg["t_end"] - clock.t)
        clock.advance(seg["t_end"])
        hub_in.publish(topic, {"type": "segment", **seg})
        engine.pump()
    hub_in.publish(topic, {"type": "ended", "call_id": call_id})
    engine.pump()
    if not engine.finalized:
        engine.finalize()
    engine._close()
    summary = engine.summary()
    summary["segments"] = len(segments)
    summary["timed"] = all(t["t_start"] is not None for t in turns)
    return summary
