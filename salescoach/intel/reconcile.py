"""Disagreement between agents, kept and reconciled.

The call analyst reads one call; the strategist reads the whole deal. When
they see momentum, urgency, champion strength or buying intent differently,
both stances stay in `assessments` untouched. This module adds a
`reconciliations` row that cites BOTH assessment ids and states the reconciled
verdict and why. It also compares the strategist's latest read with its
previous one (earlier vs latest), so a quiet change of mind is visible.

The reconciler is a small sonnet-class agent. If it fails, a deterministic
fallback applies: the more conservative stance wins and the rationale quotes
both. A deal page must never lose a disagreement because a model was down.
"""
import json
import logging

from ..agents.base import AgentFailed
from ..store.stores import now
from ..validators import evidence as ev
from . import tables
from .agentkit import IntelAgent
from .schemas import AssessmentReconciliation

log = logging.getLogger("salescoach.intel")
STRATEGIST = tables.ACTOR
RANK = {"none": 0, "weak": 1, "unknown": 1, "moderate": 2, "strong": 3}
# Lowest level whose words appear wins: a fallback should lean conservative.
LEVEL_WORDS = [
    ("none", ("no urgency", "no momentum", "no buying", "no intent", "no champion", "none", "absent", "zero")),
    ("weak", ("weak", "stall", "unproven", "untested", "low", "slipp", "regress", "declin", "unclear", "soft",
              "fragile", "not yet")),
    ("moderate", ("moderate", "medium", "some", "conditional", "mixed", "partial", "present", "cautious", "engaged")),
    ("strong", ("strong", "high", "clear", "committed", "advanc", "solid")),
]


def classify(stance: str) -> str:
    text = " " + ev.normalize(stance) + " "
    for level, words in LEVEL_WORDS:
        if any(w in text for w in words):
            return level
    return "unknown"


class ReconcilerAgent(IntelAgent):
    name = "assessment_reconciler"
    schema = AssessmentReconciliation

    def build_prompt(self, ctx):
        lines = [f"DEAL: {ctx['deal_name']}", "", "PAIRS"]
        for p in ctx["pairs"]:
            a, b = p["first"], p["second"]
            lines += [f"## pair_id {p['id']} | subject {p['subject'].replace('_', ' ')} | {p['kind'].replace('_', ' ')}",
                      f"A ({a['agent'].replace('_', ' ')}, {a['created_at'][:10]}, call {a['call_id']}): {a['stance']}"
                      f" | confidence {a['confidence']} | rationale: {a['rationale'] or '-'}",
                      f"B ({b['agent'].replace('_', ' ')}, {b['created_at'][:10]}, call {b['call_id']}): {b['stance']}"
                      f" | confidence {b['confidence']} | rationale: {b['rationale'] or '-'}", ""]
        return "\n".join(lines)


def _pairs(conn, deal_id, call_id, ids) -> list[dict]:
    pairs = []
    for subject, sid in ids.items():
        full = f"deal:{deal_id}/{subject}"
        s = conn.execute("SELECT * FROM assessments WHERE id=?", (sid,)).fetchone()
        if s is None:
            continue
        a = conn.execute("SELECT * FROM assessments WHERE subject=? AND agent='call_analyst' "
                         "ORDER BY (call_id=?) DESC, created_at DESC, id DESC LIMIT 1", (full, call_id)).fetchone()
        if a and ev.normalize(a["stance"]) != ev.normalize(s["stance"]):
            pairs.append({"id": f"{subject}/analyst", "subject": subject, "kind": "analyst_vs_strategist",
                          "first": dict(a), "second": dict(s)})
        p = conn.execute("SELECT * FROM assessments WHERE subject=? AND agent=? AND (call_id IS NULL OR call_id!=?) "
                         "ORDER BY created_at DESC, id DESC LIMIT 1", (full, STRATEGIST, call_id)).fetchone()
        if p and ev.normalize(p["stance"]) != ev.normalize(s["stance"]):
            pairs.append({"id": f"{subject}/earlier", "subject": subject, "kind": "earlier_vs_latest",
                          "first": dict(p), "second": dict(s)})
    return pairs


def _levels(conn, deal_id, call_id, result) -> tuple[dict, dict]:
    current = {a["subject"]: a.get("level") for a in (result or {}).get("assessments", [])}
    prior, _ = tables.latest_strategy(conn, deal_id, exclude_call=call_id)
    earlier = {a["subject"]: a.get("level") for a in (prior or {}).get("assessments", [])}
    return current, earlier


def fallback(pair, current, earlier, why) -> dict | None:
    a, b = pair["first"], pair["second"]
    la = earlier.get(pair["subject"]) if pair["kind"] == "earlier_vs_latest" else None
    la = la or classify(a["stance"])
    lb = current.get(pair["subject"]) or classify(b["stance"])
    if RANK[la] == RANK[lb]:
        return None
    winner, level = (a, la) if RANK[la] < RANK[lb] else (b, lb)
    rationale = (f"Deterministic fallback ({why}). Kept the more conservative stance. "
                 f"{a['agent'].replace('_', ' ').capitalize()} said: \"{a['stance']}\" ({la}). "
                 f"{b['agent'].replace('_', ' ').capitalize()} said: \"{b['stance']}\" ({lb}).")
    return {"verdict": winner["stance"], "level": level, "rationale": rationale, "method": "fallback"}


def run(conn, deal_id, call_id, ids: dict, result: dict | None = None) -> list[int]:
    """Reconcile the strategist's fresh assessments. Returns the reconciliation row ids written."""
    pairs = _pairs(conn, deal_id, call_id, ids)
    if not pairs:
        return []
    deal = conn.execute("SELECT name FROM deals WHERE node_id=?", (deal_id,)).fetchone()
    ctx = {"deal_id": deal_id, "call_id": call_id, "deal_name": deal["name"] if deal else deal_id, "pairs": pairs}
    verdicts, why = {}, None
    try:
        out, _, _, _ = ReconcilerAgent().run(conn, ctx)
        verdicts = {v.pair_id: v.model_dump() for v in out.verdicts}
    except AgentFailed as exc:
        why = f"the reconciliation model was unavailable: {str(exc)[:160]}"
        log.warning("reconciliation fell back for %s: %s", deal_id, exc)
    current, earlier = _levels(conn, deal_id, call_id, result)
    written = []
    for p in pairs:
        v = verdicts.get(p["id"])
        if v is None:
            decided = fallback(p, current, earlier, why or "the model gave no verdict for this pair")
        elif v["disagree"]:
            note = " (the later read reflects new evidence)" if v["changed_by_new_evidence"] else ""
            decided = {"verdict": v["verdict"], "level": v["level"], "rationale": v["rationale"] + note,
                       "method": "model"}
        else:
            decided = None
        if decided is None:
            continue
        cur = conn.execute(
            "INSERT INTO reconciliations(subject,verdict,rationale,assessment_ids,created_at) VALUES (?,?,?,?,?)",
            (f"deal:{deal_id}/{p['subject']}", decided["verdict"], decided["rationale"],
             json.dumps([p["first"]["id"], p["second"]["id"]]), now()))
        written.append(cur.lastrowid)
    return written
