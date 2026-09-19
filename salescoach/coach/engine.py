"""LiveCoach: one call's coach, from transcript segments to at most one nudge.

  hub topic call:<id> --segment--> state -> fast detectors --+
                                        \-> slow pass (thread) -+-> ranker -> nudge
                                                                    |
                    nudges table (every candidate, shown or not) <--+--> hub coach:<id>, coach:current

Why it is shaped this way:
  * One engine thread owns the state, the ranker and the sqlite handle. The
    slow pass and the optional local classifier run elsewhere and hand their
    results back through the same inbox queue the hub fills, so nothing is
    shared across threads and the fast path never waits on a model.
  * Time is call time (seconds since the call started), read from a clock
    object. Live, the clock follows segment timestamps and runs on the
    monotonic clock between them; a replay drives a virtual clock, which is
    what lets a 60-minute call be tuned in minutes with the same rules.
  * The slow pass result carries its own latency. Live, it is applied when
    it lands; in a replay it is held until the virtual clock reaches
    snapshot + latency, so a replay sees the same staleness a live call would.
  * Every candidate is persisted when it is retired or shown, with its
    reason. The post-call timeline and the tuning loop read that table.
  * On `ended`, each shown nudge gets an outcome (did ME act on it within a
    minute?) and one IMPORTANT_SIGNAL_DETECTED event on the durable bus, in
    one commit. Replays write nudges but no bus events.
"""
import logging
import queue
import threading
import time
import uuid
from typing import Callable, Optional

from ..orchestrator import bus
from ..schemas.events import Event
from ..store import stores
from . import settings
from .detectors import FastDetectors, LocalClassifier, make_candidate
from .ranker import Ranker
from .slow_pass import SlowPass, SlowResult, load_deal_context
from .state import ConversationState, Seg
from .text import Vocab, content_words, jaccard

log = logging.getLogger("salescoach.coach")
CURRENT_TOPIC = "coach:current"


def coach_topic(call_id: str) -> str:
    return f"coach:{call_id}"


def call_topic(call_id: str) -> str:
    return f"call:{call_id}"


def publish_status(hub, state: str, call_id: Optional[str] = None, title: Optional[str] = None,
                   mode: str = "live") -> None:
    """idle | listening, on coach:current (the overlay and the live page read it)."""
    msg = {"type": "status", "state": state, "mode": mode}
    if call_id:
        msg.update(call_id=call_id, title=title)
    hub.publish(CURRENT_TOPIC, msg)


class LiveClock:
    """Call seconds for a live call: follows segment timestamps, and runs on
    the monotonic clock in between so cooldowns expire during silence."""

    def __init__(self):
        self._t, self._m = 0.0, None

    def observe(self, t_call: float) -> None:
        if t_call > self():
            self._t, self._m = float(t_call), time.monotonic()

    def __call__(self) -> float:
        return self._t + (time.monotonic() - self._m if self._m is not None else 0.0)


class ReplayClock:
    """A virtual clock the replay feeder moves forward."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def observe(self, t_call: float) -> None:
        self.t = max(self.t, float(t_call))

    advance = observe

    def __call__(self) -> float:
        return self.t


def learned_focus(conn) -> Optional[dict]:
    """learning.feedback.live_focus, or None: the live coach must start whatever state the learner is in."""
    try:
        from ..learning import feedback
        return feedback.live_focus(conn)
    except Exception:
        log.exception("learned live-coach focus failed; starting without it")
        return None


class LiveCoach:
    def __init__(self, call_id: str, db_path=None, hub=None, provider=None, *, cfg: Optional[dict] = None,
                 overrides: Optional[dict] = None, publish_hub=None, clock=None, mode: str = "live",
                 slow: bool = True, slow_mode: str = "thread", emit_events: bool = True,
                 publish_current: bool = True, session: Optional[str] = None,
                 classify: Optional[Callable] = None):
        if hub is None:
            from ..live import hub as hub_module
            hub = hub_module.hub
        self.call_id, self.db_path, self.mode = call_id, db_path, mode
        conn = stores.sales(db_path)
        try:
            self.deal = load_deal_context(conn, call_id)
            # Phase F2: the seller's top learned weakness that a live trigger counters. None when nothing
            # is learned, the feed is off, or the caller brought a finished cfg (then nothing is retuned).
            self.focus = learned_focus(conn) if cfg is None else None
        finally:
            conn.close()
        self.cfg = cfg or settings.load(overrides, learned={self.focus["trigger"]: self.focus["boost"]}
                                        if self.focus else None)
        self.vocab = Vocab(self.cfg)
        self.hub, self.out_hub = hub, publish_hub or hub
        self.clock = clock or LiveClock()
        self.slow_mode, self.emit_events, self.publish_current = slow_mode, emit_events, publish_current
        self.session = session or f"{mode}-{uuid.uuid4().hex[:8]}"
        self.state = ConversationState(self.cfg, self.vocab, known_people=self.deal["known_people"])
        self.detectors = FastDetectors(self.cfg, self.vocab)
        self.ranker = Ranker(self.cfg)
        self.slow = SlowPass(self.cfg, call_id, db_path, provider=provider, deal=self.deal, mode=mode,
                             focus=self.focus) if slow and self.cfg["slow"].get("enabled", True) else None
        self.local = LocalClassifier(self.cfg, self._on_local, classify)
        self.stats = {"segments": 0, "candidates": 0, "shown": 0, "suppressed": 0, "fast_ms_max": 0.0,
                      "slow_passes": 0, "slow_errors": 0, "slow_late": 0, "slow_latency_s": [], "local": 0}
        self.slow_log: list[dict] = []
        self.finalized = False
        self._q: Optional[queue.Queue] = None
        self._conn = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._done = threading.Event()
        self._seen: set = set()
        self._slow_running = False
        self._slow_started = 0
        self._last_slow_t = 0.0
        self._segs_since_slow = 0
        self._pending_slow: list[SlowResult] = []
        self._last_snapshot_t = 0.0

    # ---- lifecycle ------------------------------------------------------------

    def attach(self, threaded: bool = True, backfill: bool = True) -> "LiveCoach":
        self._q = self.hub.subscribe(call_topic(self.call_id), maxsize=5000)
        if backfill and self.mode == "live":
            self._backfill()
        if threaded:
            self._thread = threading.Thread(target=self._loop, name=f"coach-{self.call_id}", daemon=True)
            self._thread.start()
        return self

    def _backfill(self) -> None:
        """Live turns already saved (the engine attached a moment late, or the server restarted mid-call)."""
        conn = stores.sales(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM turns WHERE call_id=? AND tier='live' ORDER BY idx",
                                (self.call_id,)).fetchall()
        finally:
            conn.close()
        for r in rows:
            self._inbox({"type": "segment", "idx": r["idx"], "channel": r["channel"], "t_start": r["t_start"],
                         "t_end": r["t_end"], "text": r["text"]})

    def _loop(self) -> None:
        try:
            while not self._stop.is_set() and not self.finalized:
                try:
                    message = self._q.get(timeout=0.5)
                except queue.Empty:
                    message = None
                try:
                    if message is not None:
                        self.handle(message)
                    if not self.finalized:
                        self.tick()
                except Exception:
                    log.exception("live coach step failed for %s", self.call_id)
        finally:
            if not self.finalized:
                try:
                    self.finalize()
                except Exception:
                    log.exception("live coach finalize failed for %s", self.call_id)
            self._close()

    def pump(self) -> None:
        """Replays and tests: process everything queued, synchronously."""
        while self._q is not None and not self.finalized:
            try:
                message = self._q.get_nowait()
            except queue.Empty:
                break
            self.handle(message)
        if not self.finalized:
            self.tick()

    def end(self) -> None:
        """Ask the engine to finalize (from any thread)."""
        if self._thread is not None:
            self._inbox({"type": "ended", "call_id": self.call_id, "source": "coach"})
        elif not self.finalized:
            self.finalize()
            self._close()

    def wait(self, timeout: float = 10.0) -> bool:
        return self._done.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def dismiss(self, nudge_id: int) -> None:
        self._inbox({"type": "_dismiss", "id": int(nudge_id)})

    def _inbox(self, message: dict) -> None:
        if self._q is None:
            return
        while True:
            try:
                self._q.put_nowait(message)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def _db(self):
        if self._conn is None:
            self._conn = stores.sales(self.db_path)
        return self._conn

    def _close(self) -> None:
        if self._q is not None:
            self.hub.unsubscribe(call_topic(self.call_id), self._q)
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._done.set()

    # ---- messages -------------------------------------------------------------

    def handle(self, message: dict) -> None:
        kind = message.get("type")
        if self.finalized:
            return
        if kind == "segment":
            self._on_segment(message)
        elif kind == "ended":
            self.finalize()
        elif kind == "_dismiss":
            self.ranker.dismiss(message["id"])
        elif kind == "_slow":
            self._slow_running = False
            self._pending_slow.append(message["result"])
            self.tick()
        elif kind == "_local":
            self._local_candidate(message)

    def _on_segment(self, m: dict) -> None:
        text = (m.get("text") or "").strip()
        if not text:
            return
        idx = m.get("idx")
        if idx is not None:
            if idx in self._seen:
                return
            self._seen.add(idx)
        channel = m.get("channel") if m.get("channel") in ("me", "them") else "them"
        t_start = float(m["t_start"]) if m.get("t_start") is not None else self.clock()
        t_end = float(m["t_end"]) if m.get("t_end") is not None else t_start
        self.clock.observe(t_end)
        started = time.perf_counter()
        seg = Seg(idx if idx is not None else len(self.state.segments), channel, t_start, t_end, text)
        self.state.add(seg)
        candidates = self.detectors.detect(self.state, seg)
        self.stats["fast_ms_max"] = max(self.stats["fast_ms_max"], round((time.perf_counter() - started) * 1000, 2))
        self.stats["segments"] += 1
        if not candidates and self.local.wants(seg):
            self.local.submit(seg, t_end)
        for c in candidates:
            self._offer(c)
        self._segs_since_slow += 1
        strong = any(c.confidence >= float(self.cfg["slow"].get("strong_signal", 0.8)) for c in candidates)
        self._maybe_slow(strong)
        self._rank()

    def tick(self) -> None:
        now = self.clock()
        for r in list(self._pending_slow):
            if self.slow_mode == "thread" or r.arrive_at <= now + 1e-6:
                self._pending_slow.remove(r)
                self._apply_slow(r)
        self._maybe_slow(False)
        self._rank()
        if now - self._last_snapshot_t >= 120 and self.state.segments:
            self._snapshot(now)

    # ---- candidates -----------------------------------------------------------

    def _offer(self, c) -> None:
        self.stats["candidates"] += 1
        self._retire(self.ranker.offer(c))

    def _rank(self) -> None:
        now = self.clock()
        winner, retired = self.ranker.select(now, self.state)
        self._retire(retired)
        if winner is not None:
            self._show(winner, now)

    def _retire(self, candidates) -> None:
        for c in candidates:
            self.stats["suppressed"] += 1
            self._persist(c, retired_at=self.clock())

    def _show(self, c, now: float) -> None:
        c.shown_wall = stores.now()
        try:
            self._persist(c)
        except Exception:
            # Not stored means not shown: give the ranker its cooldown and budget slot back.
            log.exception("nudge %s could not be stored; not shown", c.trigger)
            self.ranker.unshow(c)
            return
        self.stats["shown"] += 1
        label = self.cfg["triggers"][c.trigger].get("label", c.trigger)
        msg = {"type": "nudge", "id": c.id, "call_id": self.call_id, "session": self.session, "mode": self.mode,
               "trigger": c.trigger, "label": label, "text": c.text, "kind": c.kind, "source": c.source,
               "score": c.score, "confidence": c.confidence, "t_call": round(now, 1),
               "ttl_s": float(self.cfg["budget"]["display_s"])}
        self.out_hub.publish(coach_topic(self.call_id), msg)
        if self.publish_current:
            self.out_hub.publish(CURRENT_TOPIC, msg)

    def _persist(self, c, retired_at: Optional[float] = None) -> None:
        conn = self._db()
        t_call = c.shown_at if c.shown else (retired_at if retired_at is not None else c.created_at)
        cur = conn.execute(
            "INSERT INTO nudges(call_id,session,mode,t_call,raised_t,trigger,text,kind,source,score,confidence,shown,"
            "suppressed_reason,anchor_text,anchor_t,entity,rationale,urgency,shown_wall,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.call_id, self.session, self.mode, round(t_call, 2), round(c.created_at, 2), c.trigger, c.text,
             c.kind, c.source, round(c.score, 4), c.confidence, int(c.shown), c.suppressed_reason,
             c.anchor_text[:300] or None, c.anchor_t, c.entity, c.rationale[:500] or None, c.urgency,
             c.shown_wall, stores.now()))
        conn.commit()
        c.id = cur.lastrowid

    # ---- slow path ------------------------------------------------------------

    def _maybe_slow(self, strong: bool) -> None:
        if self.slow is None or self._slow_running or self.finalized:
            return
        s = self.cfg["slow"]
        cap = int(s.get("max_passes") or 0)
        if cap and self._slow_started >= cap:
            return                                         # a capped replay: the fast rules carry on alone
        now = self.clock()
        gap = now - self._last_slow_t
        due = gap >= float(s["interval_s"]) or (strong and gap >= float(s["min_gap_s"]))
        if not due or self._segs_since_slow < int(s.get("min_new_segments", 3)):
            return
        ctx = self.slow.context(self.state, now, self.ranker.shown)
        self._last_slow_t, self._segs_since_slow = now, 0
        self._slow_started += 1
        if self.slow_mode == "sync":
            self._pending_slow.append(self.slow.run(ctx))
        else:
            self._slow_running = True
            threading.Thread(target=self._slow_thread, args=(ctx,), name="coach-slow-pass", daemon=True).start()

    def _slow_thread(self, ctx: dict) -> None:
        try:
            result = self.slow.run(ctx)
        except Exception as exc:                       # run() already catches; belt and braces
            result = SlowResult(t_snapshot=ctx["t_call"], latency_s=0.0, error=str(exc)[:300])
        self._inbox({"type": "_slow", "result": result})

    def _apply_slow(self, r: SlowResult) -> None:
        self.stats["slow_passes"] += 1
        self.stats["slow_latency_s"].append(r.latency_s)
        now = self.clock()
        entry = {"t": round(r.t_snapshot, 1), "latency_s": r.latency_s, "run_id": r.run_id, "error": r.error}
        self.slow_log.append(entry)
        if r.error or r.output is None:
            self.stats["slow_errors"] += 1
            return
        out = r.output
        age = r.latency_s if self.slow_mode == "sync" else max(r.latency_s, now - r.t_snapshot)
        late = age > float(self.cfg["slow"]["max_age_s"])
        for u in out.state:
            self.state.mark(u.slot, u.status, u.value, "slow", now)
        self.state.set_phase(out.phase, "slow", now)
        keep, dropped = self.slow.interventions(out)
        entry.update(phase=out.phase, interventions=[iv.trigger for iv in out.interventions], late=late)
        if late:
            self.stats["slow_late"] += 1
        for iv, reason in [(iv, "slow_late" if late else None) for iv in keep] + dropped:
            c = self._slow_candidate(iv, r)
            if reason is None and r.versions.get(c.slot, 0) < self.state.slots[c.slot].asks:
                reason = "resolved"                        # ME asked about it while the pass was running
            self.stats["candidates"] += 1
            if reason:
                c.suppressed_reason = reason
                self._retire([c])
            else:
                self._retire(self.ranker.offer(c))
        self._snapshot(now, slow=entry)

    def _slow_candidate(self, iv, r: SlowResult):
        conf = float((self.cfg["slow"].get("urgency_confidence") or {}).get(iv.urgency, 0.6))
        seg = self._anchor(iv.anchor_quote, r.t_snapshot)
        return make_candidate(self.cfg, iv.trigger, conf, r.t_snapshot, seg, self.state, source="slow",
                              text=iv.text.strip(), rationale=iv.rationale, urgency=iv.urgency)

    def _anchor(self, quote: str, t_snapshot: float) -> Optional[Seg]:
        window = float(self.cfg["slow"]["window_s"])
        segs = [s for s in self.state.segments if t_snapshot - window <= s.t_end <= t_snapshot + 1]
        if not segs:
            return None
        words = content_words(self.vocab.neutralise((quote or "").lower()), self.vocab.stop)
        best, best_score = None, 0.0
        for s in segs:
            score = jaccard(words, content_words(s.norm, self.vocab.stop))
            if score > best_score:
                best, best_score = s, score
        if best is not None and best_score >= 0.2:
            return best
        return next((s for s in reversed(segs) if s.channel == "them"), segs[-1])

    # ---- optional local classifier -------------------------------------------

    def _on_local(self, trigger: str, confidence: float, seg: Seg, now: float) -> None:
        self._inbox({"type": "_local", "trigger": trigger, "confidence": confidence, "seg": seg, "t": now})

    def _local_candidate(self, m: dict) -> None:
        self.stats["local"] += 1
        c = make_candidate(self.cfg, m["trigger"], m["confidence"], m["t"], m["seg"], self.state,
                           rationale="local model (llama3.2:3b)")
        self._offer(c)
        self._rank()

    # ---- end of call ----------------------------------------------------------

    def finalize(self) -> dict:
        if self.finalized:
            return self.summary()
        now = self.clock()
        self._retire(self.ranker.flush("call_ended"))
        conn = self._db()
        window = float(self.cfg["outcome"]["window_s"])
        for c in self.ranker.shown:
            c.outcome, c.outcome_evidence = self._outcome(c, now, window)
            conn.execute("UPDATE nudges SET outcome=?, outcome_evidence=?, dismissed=MAX(dismissed, ?) WHERE id=?",
                         (c.outcome, c.outcome_evidence[:300] or None, int(c.dismissed), c.id))
            if self.emit_events:
                bus.publish(conn, Event(
                    type="IMPORTANT_SIGNAL_DETECTED", entity_id=self.call_id,
                    dedupe_key=f"IMPORTANT_SIGNAL_DETECTED:{self.call_id}:{c.id}",
                    payload={"call_id": self.call_id, "nudge_id": c.id, "trigger": c.trigger, "text": c.text,
                             "ts": c.shown_wall, "t_call": round(c.shown_at, 1), "outcome": c.outcome}))
        self._snapshot(now, final=True, commit=False)
        conn.commit()
        self.finalized = True
        self.out_hub.publish(coach_topic(self.call_id), {"type": "coach_ended", "call_id": self.call_id,
                                                         "session": self.session})
        return self.summary()

    def _outcome(self, c, end: float, window: float) -> tuple[str, str]:
        cues = self.vocab.asks.get(c.slot)
        min_words = int(self.cfg["fast"].get("min_me_words", 4))
        mine = [s for s in self.state.segments
                if s.channel == "me" and s.t_end > c.shown_at and s.t_start <= c.shown_at + window]
        for s in mine:
            if (cues is not None and cues.any(s.norm)) or (c.entity and c.trigger == "stakeholder_gap"
                                                             and c.entity in s.norm and self.state.is_question(s)):
                return "followed", s.text
        if any(s.n_words >= min_words for s in mine):
            return "ignored", ""
        return "unknown", ""

    def _snapshot(self, now: float, final: bool = False, slow: Optional[dict] = None, commit: bool = True) -> None:
        import json
        snap = self.state.snapshot()
        if slow:
            snap["slow"] = slow
        if final:
            snap["final"] = True
            snap["stats"] = {k: v for k, v in self.stats.items() if k != "slow_latency_s"}
        conn = self._db()
        conn.execute("INSERT INTO coach_state(call_id,session,t_call,json,created_at) VALUES (?,?,?,?,?)",
                     (self.call_id, self.session, round(now, 2), json.dumps(snap, default=str), stores.now()))
        if commit:
            conn.commit()
        self._last_snapshot_t = now

    def summary(self) -> dict:
        shown = self.ranker.shown
        rated = [c for c in shown if c.outcome in ("followed", "ignored")]
        lat = self.stats["slow_latency_s"]
        return {
            "call_id": self.call_id, "session": self.session, "mode": self.mode,
            "t_end": round(self.clock(), 1),
            "shown": [{"id": c.id, "t": round(c.shown_at, 1), "trigger": c.trigger, "text": c.text,
                       "source": c.source, "score": c.score, "outcome": c.outcome, "anchor": c.anchor_text[:160]}
                      for c in shown],
            "adherence": round(sum(c.outcome == "followed" for c in rated) / len(rated), 2) if rated else None,
            "stats": {**{k: v for k, v in self.stats.items() if k != "slow_latency_s"},
                      "slow_latency_avg_s": round(sum(lat) / len(lat), 1) if lat else None},
        }
