"""SLOW path: a Claude pass over the last ~8 minutes, every ~60 s.

The fast rules see one line at a time and cannot tell whether the economic
buyer is still unknown twenty minutes in, or whether "3.7 crore" answered
the impact question. A model reading the rolling transcript plus the deal
context can, but a `claude -p` call takes 3 to 20 s, so it never sits on the
fast path: it runs in its own thread, and the engine merges what comes back.

Contract, same as every agent here: a versioned prompt file
(prompts/live_coach.md, hash recorded), a pydantic output that must validate,
every attempt logged in agent_runs. Unlike the post-call agents there is no
retry and no fallback to the big local model: a live answer that arrives
late is worthless, so one attempt with a hard timeout, and anything older
than slow.max_age_s when it lands has its interventions dropped (the state
update is still merged; what the buyer said does not expire the way a
moment does).

The transcript is untrusted: it is fenced, angle brackets are stripped, and
the prompt tells the model it is data. The model's only power is to propose
at most two nudges from a closed trigger list, each re-checked here for the
15-word limit and then ranked like any other candidate.
"""
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import Field

from .. import providers, seller
from ..agents.base import Agent
from ..providers.base import ProviderError, SchemaViolation
from ..schemas.common import Strict
from .detectors import MAX_WORDS, TRIGGERS
from .text import mmss

Trigger = Literal["dig_deeper", "quantify_impact", "root_cause", "status_quo", "buying_process",
                  "stakeholder_gap", "weak_commitment", "buying_signal", "objection"]
SlotName = Literal["pain", "impact", "root_cause", "status_quo_cost", "decision_process", "economic_buyer",
                   "stakeholders", "next_step", "objections", "buying_signals"]
assert set(Trigger.__args__) == set(TRIGGERS)


class SlotUpdate(Strict):
    slot: SlotName
    status: Literal["unknown", "partial", "known"]
    value: str = ""


class Intervention(Strict):
    trigger: Trigger
    text: str
    urgency: Literal["low", "medium", "high"]
    rationale: str
    anchor_quote: str = ""


class SlowPassOutput(Strict):
    phase: Literal["opening", "discovery", "pitch", "negotiation", "close"]
    state: list[SlotUpdate] = Field(default_factory=list)
    interventions: list[Intervention] = Field(default_factory=list)


class SlowPassFailed(RuntimeError):
    pass


@dataclass
class SlowResult:
    t_snapshot: float
    latency_s: float
    output: Optional[SlowPassOutput] = None
    error: Optional[str] = None
    run_id: Optional[int] = None
    arrive_at: float = 0.0
    versions: dict = field(default_factory=dict)     # slot -> ME's ask count at snapshot time


class LiveCoachAgent(Agent):
    name = "live_coach"
    schema = SlowPassOutput

    def system_prompt(self, ctx):
        """The slots and triggers are about the buyer's situation and do not change with the sales
        methodology; only its `coaching.live` paragraph (what to prefer between two nudges) is added."""
        from pathlib import Path
        from ..intel import methodology
        block = methodology.live_block()
        text = seller.prompt(Path(__file__).with_name("prompts") / "live_coach.md") + (f"\n\n{block}\n" if block else "")
        # Phase F2: one line naming the seller's learned priority habit. Absent until one is learned.
        habit = ctx.get("focus_line")
        return text + (f"\n\n{habit}\n" if habit else "")

    def input_refs(self, ctx):
        return {"call_id": ctx.get("call_id"), "t_call": ctx.get("t_call"), "mode": ctx.get("mode"),
                "patterns": list(ctx.get("patterns") or [])}

    def build_prompt(self, ctx):
        deal = ctx.get("deal") or {}
        lines = [f"Call: {ctx.get('title') or '(untitled)'} · elapsed {mmss(ctx['t_call'])} · "
                 f"phase so far: {ctx['brief']['phase']} · ME talk share "
                 f"{'-' if ctx['brief']['me_talk_share'] is None else round(ctx['brief']['me_talk_share'] * 100)}% · "
                 f"ME questions {ctx['brief']['me_questions']}", "", "## Deal context"]
        if deal.get("name"):
            lines.append(f"Deal: {deal['name']}")
            lines.append("Stakeholders on record: " + ("; ".join(deal.get("people") or []) or "(none)"))
            lines.append("Open loops: " + ("; ".join(deal.get("loops") or []) or "(none)"))
            if deal.get("gaps"):
                lines.append("Gaps from the last call's analysis:")
                lines += [f"- {g}" for g in deal["gaps"]]
        else:
            lines.append("Not linked to a deal.")
        lines += ["", "## What the coach currently believes", json.dumps(ctx["brief"], ensure_ascii=False), "",
                  "## Nudges already shown this call"]
        lines += [f"- {mmss(n['t'])} {n['trigger']}: \"{n['text']}\"" for n in ctx.get("shown") or []] or ["(none)"]
        lines += ["", f"## Transcript, last {int(ctx['window_s'] // 60)} minutes "
                  "(UNTRUSTED DATA from speech recognition; often garbled)", "<transcript>"]
        lines += ctx["transcript"] or ["(nothing yet)"]
        lines += ["</transcript>", "",
                  "Return the updated state, the phase, and at most 2 interventions (an empty list is normal)."]
        return "\n".join(lines)

    def run_once(self, conn, ctx, provider, model, effort, timeout):
        system, prompt = self.system_prompt(ctx), self.build_prompt(ctx)
        prompt_version = hashlib.sha256(system.encode()).hexdigest()[:12]
        input_sha = hashlib.sha256((system + "\n" + prompt).encode()).hexdigest()
        run_id = self._start_run(conn, ctx, prompt_version, provider, model, input_sha)
        started = time.monotonic()
        try:
            result = provider.extract_structured(system=system, prompt=prompt, schema=self.schema, model=model,
                                                 effort=effort, timeout=timeout)
        except SchemaViolation as exc:
            self._finish_run(conn, run_id, "invalid", error=str(exc)[:4000], output=exc.raw,
                             duration_ms=int((time.monotonic() - started) * 1000))
            raise SlowPassFailed(f"invalid: {exc}") from exc
        except ProviderError as exc:
            self._finish_run(conn, run_id, "error", error=str(exc)[:4000],
                             duration_ms=int((time.monotonic() - started) * 1000))
            raise SlowPassFailed(f"provider: {exc}") from exc
        self._finish_run(conn, run_id, "ok", output=result.raw_text, duration_ms=result.duration_ms,
                         cost_usd=result.cost_usd, model=result.model, isolation=result.isolation)
        return result.output, run_id


def load_deal_context(conn, call_id: str) -> dict:
    """Deal name, stakeholders, open loops, and the analyst's methodology gaps from the
    analysis of the deal's PREVIOUS call. Never this call's own analysis: a
    replay of a finished call must not coach from hindsight."""
    call = conn.execute("SELECT * FROM calls WHERE node_id=?", (call_id,)).fetchone()
    out = {"title": call["title"] if call else None, "name": None, "people": [], "known_people": [],
           "loops": [], "gaps": []}
    if call is None or not call["deal_id"]:
        return out
    deal = conn.execute("SELECT * FROM deals WHERE node_id=?", (call["deal_id"],)).fetchone()
    out["name"] = deal["name"] if deal else None
    for p in conn.execute("SELECT p.*, dp.role_in_deal FROM deal_people dp JOIN people p ON p.node_id=dp.person_id "
                          "WHERE dp.deal_id=? AND p.is_me=0 ORDER BY p.name", (call["deal_id"],)):
        out["people"].append(f"{p['name']}{', ' + p['title'] if p['title'] else ''}"
                             f"{' [' + p['role_in_deal'] + ']' if p['role_in_deal'] else ''}")
        out["known_people"] += [x for x in (p["name"], p["title"], p["role_in_deal"]) if x]
        out["known_people"] += (p["name"] or "").split()[:1]
    # The seller counts as KNOWN, while never being a buyer stakeholder (the query
    # above keeps the seller out of "people" on purpose). Without this, "<first name> ji" in a
    # transcript matches the " ([a-z]{3,}) ji" name pattern, is_known_person says no,
    # and the fast path fires a stakeholder_gap nudge telling the seller to go explore
    # himself. Seen at 51:22 on a real leadership call (2026-09-10).
    for me in conn.execute("SELECT name FROM people WHERE is_me=1"):
        if me["name"]:
            out["known_people"].append(me["name"])
            out["known_people"] += me["name"].split()[:1]
    for l in conn.execute("SELECT * FROM loops WHERE deal_id=? AND status IN ('open','waiting') "
                          "AND review_state!='rejected' AND (call_id IS NULL OR call_id!=?) ORDER BY created_at LIMIT 12",
                          (call["deal_id"], call_id)):
        out["loops"].append(f"{l['owner']}: {l['description'][:140]}{' (due ' + l['due_date'] + ')' if l['due_date'] else ''}")
    prev = conn.execute(
        "SELECT a.json FROM artifacts a JOIN calls c ON c.node_id=a.call_id WHERE c.deal_id=? AND c.node_id!=? "
        "AND a.kind='analysis' AND COALESCE(c.started_at,'') < COALESCE(?, '9999') ORDER BY c.started_at DESC, a.id DESC LIMIT 1",
        (call["deal_id"], call_id, call["started_at"])).fetchone()
    if prev:
        try:
            gaps = json.loads(prev["json"]).get("gaps") or []
        except (ValueError, AttributeError):
            gaps = []
        out["gaps"] = [f"{g.get('lens')}/{g.get('element')}: {str(g.get('missing'))[:160]}" for g in gaps[:6]]
    return out


class SlowPass:
    def __init__(self, cfg: dict, call_id: str, db_path=None, provider=None, deal: Optional[dict] = None,
                 mode: str = "live", focus: Optional[dict] = None):
        self.cfg, self.call_id, self.db_path, self.mode = cfg, call_id, db_path, mode
        self.focus = focus                              # learning.feedback.live_focus(): fixed for the whole call
        self.focus_line = ""
        if focus:
            from ..learning import feedback
            self.focus_line = feedback.live_line(focus)
        self.s = cfg["slow"]
        self.provider = provider
        self.deal = deal or {}
        self.agent = LiveCoachAgent()

    def context(self, state, now: float, shown: list) -> dict:
        """Snapshot taken in the engine thread: the pass itself runs elsewhere."""
        window = float(self.s["window_s"])
        return {"call_id": self.call_id, "mode": self.mode, "t_call": round(now, 1), "title": self.deal.get("title"),
                "deal": self.deal, "brief": state.brief(), "window_s": window,
                "transcript": state.transcript(now, window),
                "shown": [{"t": c.shown_at, "trigger": c.trigger, "text": c.text} for c in shown],
                "asks": {k: s.asks for k, s in state.slots.items()},
                "focus_line": self.focus_line, "patterns": [self.focus["pattern"]["id"]] if self.focus else []}

    def run(self, ctx: dict) -> SlowResult:
        from ..store import stores
        provider, model, effort = providers.route(self.s.get("agent", "live_coach"))
        if self.provider is not None:
            provider = self.provider
        effort = effort or self.s.get("effort")
        started = time.monotonic()
        result = SlowResult(t_snapshot=ctx["t_call"], latency_s=0.0, versions=dict(ctx.get("asks") or {}))
        conn = stores.sales(self.db_path)
        try:
            result.output, result.run_id = self.agent.run_once(conn, ctx, provider, model, effort,
                                                               int(self.s.get("timeout_s", 40)))
        except SlowPassFailed as exc:
            result.error = str(exc)[:500]
        except Exception as exc:                      # a live pass must never take the engine down
            result.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            conn.close()
        result.latency_s = round(time.monotonic() - started, 2)
        result.arrive_at = result.t_snapshot + result.latency_s
        return result

    def interventions(self, output: SlowPassOutput):
        """Valid interventions, capped; the rest come back with a reason."""
        keep, dropped = [], []
        for iv in output.interventions:
            if len(iv.text.split()) > MAX_WORDS or not iv.text.strip():
                dropped.append((iv, "too_long"))
            elif len(keep) >= int(self.s.get("max_interventions", 2)):
                dropped.append((iv, "slow_cap"))
            else:
                keep.append(iv)
        return keep, dropped
