"""Phase 3 Deal Strategist on a fake model: the pipeline step, the memory gate,
evidence validation, health caps, reconciliation (model and fallback),
caching, and the CLI.

The call is test_core_pipeline's CALL1; the analyst's fake says momentum is
"interest high, urgency unproven" and the strategist's fake says it is strong,
so every run has a disagreement to reconcile.
"""
import argparse
import hashlib
import re
from datetime import date, timedelta

import numpy as np
import pytest

from salescoach import repo
from salescoach.intel import embed, history, reconcile, strategist, tables
from salescoach.memory import gate
from salescoach.orchestrator import bus, worker, workflow
from salescoach.plugins import intelligence
from salescoach.providers.base import RateLimited
from salescoach.schemas.events import Event
from salescoach.sources import paste
from test_core_pipeline import CALL1, _script, _setup

SOON = (date.today() + timedelta(days=7)).isoformat()
NBA = "Send Arjun a one-page CFO brief and ask him to book the CFO meeting"


class FakeEmbedder:
    """Bag of words hashed into 64 dims: cosine similarity tracks shared words."""
    model = "fake-bow"

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def embed(self, texts, kind="document"):
        self.calls += 1
        out = []
        for t in texts:
            v = np.zeros(64, dtype=np.float32)
            for w in re.findall(r"[a-z]+", t.lower()):
                if len(w) > 2:
                    v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
            out.append(v.tolist())
        return out


@pytest.fixture(autouse=True)
def intel_env(tmp_path, monkeypatch):
    workflow.ensure_plugins()
    intelligence.register(workflow)
    embed.set_embedder(FakeEmbedder())
    (tmp_path / "contacts").mkdir(exist_ok=True)
    monkeypatch.setattr(history, "contacts_dir", lambda: tmp_path / "contacts")
    yield
    embed.set_embedder(None)


def ev(call, turns, quote):
    return {"call_id": call, "turns": turns, "quote": quote}


def ids_in(prompt):
    call = re.search(r"LATEST CALL TRANSCRIPT \((call-[\w-]+)\)", prompt).group(1)
    arjun = re.search(r"- (person-\w+) \| Arjun Kumar", prompt).group(1)
    return call, arjun


def stakeholder(**kw):
    base = {"person_id": None, "name": None, "email": None, "role": "", "influence": "unknown", "incentives": [],
            "concerns": [], "relationship_strength": "unknown", "position": "unknown", "champion_potential": "unknown",
            "ability_to_block": "unknown", "evidence": [], "confidence": "medium"}
    base.update(kw)
    return base


def strategy(call, arjun, position="champion", score=85, fabricate=False, eb_status="known", eb_conf="high",
             eb_quote="our CFO will want to see the savings split by plant"):
    arjun_quote = "we signed the contract yesterday" if fabricate else "I'll try to set up a meeting with our CFO and CEO"
    return {
        "summary": "Arjun is engaged and offered a CFO and CEO meeting. Nobody senior has been on a call.",
        "stakeholders": [
            stakeholder(person_id=arjun, name="Arjun Kumar", email="arjun@northwind.test",
                        role="Strategic sourcing head, champion", influence="medium", incentives=["lower freight cost"],
                        concerns=["data accuracy"], relationship_strength="strong", position=position,
                        champion_potential="high", ability_to_block="low", evidence=[ev(call, [3], arjun_quote)],
                        confidence="high"),
            stakeholder(role="CFO, economic buyer", influence="high", incentives=["savings by plant"],
                        ability_to_block="high", evidence=[ev(call, [1], "our CFO will want to see the savings split by plant")],
                        confidence="high"),
            stakeholder(name="Ravi Shankar", role="Plant head", evidence=[ev(call, [6], "the pilot would start at Pant Nagar")]),
            stakeholder(person_id="person-nope", name="Someone Else", role="COO"),
            stakeholder(name="Maya Iyer", role="Founder", evidence=[ev(call, [0], "Thanks for making time, Arjun")]),
        ],
        "meddpicc": [
            {"element": "economic_buyer", "status": eb_status, "what_we_know": "The CFO and CEO decide.",
             "gap": "Neither has been met.", "next_question": "Can we lock the CFO meeting — 6 Oct?",
             "evidence": [ev(call, [1], eb_quote)], "confidence": eb_conf},
            {"element": "metrics", "status": "known", "what_we_know": "Budget approved.", "gap": "",
             "next_question": "", "evidence": [ev(call, [1], "we have budget approved for this")], "confidence": "explicit"},
            {"element": "decision_process", "status": "partial", "what_we_know": "CFO and CEO must align first.",
             "gap": "Who signs after that is unknown.", "next_question": "Who signs the pilot after the CFO meeting?",
             "evidence": [ev(call, [3], "set up a meeting with our CFO and CEO")], "confidence": "medium"},
        ],
        "risks": [{"type": "weak_urgency", "severity": "high", "description": "No compelling event.", "evidence": [],
                   "mitigation": "Ask what happens if nothing changes this year."}],
        "assessments": [
            {"subject": "momentum", "level": "strong", "stance": "Strong momentum, the CFO meeting is coming",
             "confidence": "high", "rationale": "Arjun offered the meeting.",
             "evidence": [ev(call, [3], "I'll try to set up a meeting with our CFO and CEO")]},
            {"subject": "urgency", "level": "strong", "stance": "Urgent: budget is approved", "confidence": "explicit",
             "rationale": "Budget.", "evidence": [ev(call, [1], "we have budget approved for this")]},
        ],
        "next_best_action": {"action": NBA, "why_highest_leverage": "Access to the economic buyer is the bottleneck.",
                             "owner": "me", "owner_name": None, "by_when": SOON,
                             "expected_effect": "A dated CFO meeting.", "what_would_change_it": "If the CFO is not the signer.",
                             "evidence": [ev(call, [3], "I'll try to set up a meeting with our CFO and CEO")]},
        "deal_health": {"score": score, "label": "strong", "rationale": "A great call.", "confidence": "high"},
    }


def reconcile_all(system, prompt):
    return {"verdicts": [{"pair_id": p, "disagree": True, "changed_by_new_evidence": False, "level": "weak",
                          "verdict": "Interest high, momentum unproven", "rationale": "No dated commitment from the buyer."}
                         for p in re.findall(r"pair_id (\S+)", prompt)]}


def script_intel(fake, reconciler=True, **kw):
    fake.responses["DealStrategy"] = lambda s, p: strategy(*ids_in(p), **kw)
    if reconciler:
        fake.responses["AssessmentReconciliation"] = reconcile_all
    else:
        fake.responses.pop("AssessmentReconciliation", None)


def run_call(db, fake, **kw):
    deal, people = _setup(db)
    _script(fake)
    script_intel(fake, **kw)
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    row = repo.get_call(db, call)
    assert row["wf_state"] == "awaiting_review", row["wf_error"]
    return deal, call, people


def calls_of(fake, schema):
    return [c for c in fake.calls if c["schema"] == schema]


# ---- tests --------------------------------------------------------------------------

def test_step_is_inserted_after_loops_reconciled():
    assert workflow.STEP_NAMES.index("strategized") == workflow.STEP_NAMES.index("loops_reconciled") + 1
    assert workflow.STEP_LABELS["strategized"] == "Deal strategy"


def test_strategist_step_writes_every_table(db, fake_llm):
    deal, call, (arjun, me) = run_call(db, fake_llm)
    assert len(calls_of(fake_llm, "DealStrategy")) == 1
    prompt = calls_of(fake_llm, "DealStrategy")[0]["prompt"]
    assert "Validated claims" in prompt and "CFO wants plant-wise savings" in prompt and "OPEN LOOPS" in prompt

    # people: Arjun kept, the CFO created from a verified quote that names the role, the rest refused
    people = {p["name"]: p for p in repo.deal_people(db, deal)}
    assert "CFO (name unknown)" in people and people["CFO (name unknown)"]["role_in_deal"] == "CFO, economic buyer"
    assert "Ravi Shankar" not in people and "Someone Else" not in people
    assert db.execute("SELECT COUNT(*) FROM people WHERE name='Maya Iyer'").fetchone()[0] == 1
    arjun_row = db.execute("SELECT * FROM stakeholders WHERE id=?", (tables.stakeholder_id(deal, arjun),)).fetchone()
    assert (arjun_row["position"], arjun_row["confidence"]) == ("champion", "high")
    assert db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='position'",
                      (arjun_row["id"],)).fetchone()[0] == "high"
    art = db.execute("SELECT json FROM artifacts WHERE call_id=? AND kind='strategy'", (call,)).fetchone()[0]
    assert "not named in the cited turns" in art and "unknown person id person-nope" in art and "that is Maya" in art

    # MEDDPICC: all eight, the fabricated 'known' falls to unknown at low confidence
    med = {m["element"]: m for m in tables.meddpicc_rows(db, deal)}
    assert len(med) == 8 and all(m["status"] for m in med.values())
    assert (med["economic_buyer"]["status"], med["economic_buyer"]["confidence"]) == ("known", "high")
    assert "—" not in med["economic_buyer"]["next_question"]
    assert (med["metrics"]["status"], med["metrics"]["confidence"]) == ("unknown", "low")
    assert med["decision_process"]["status"] == "partial"
    assert med["paper_process"]["status"] == "unknown"          # missing from the answer, filled in

    # risks: the model's, plus single-threading from the data
    risks = {r["type"]: r for r in tables.risks(db, deal)}
    assert set(risks) == {"weak_urgency", "single_threading"} and risks["single_threading"]["source"] == "rule"

    # health: 85 capped by facts
    h = tables.health(db, deal)
    assert (h["model_score"], h["score"], h["label"]) == (85, 50, "fair")
    assert {c["rule"] for c in h["caps"]} == {"single_threaded", "meddpicc_known_below_3"}
    assert "Capped at 50" in h["rationale"] and h["next_best_action"]["action"] == NBA
    assert db.execute("SELECT COUNT(*) FROM deal_health_history WHERE deal_id=?", (deal,)).fetchone()[0] == 1

    # assessments: the analyst's row untouched, the strategist's added; fabricated evidence lowered
    rows = db.execute("SELECT * FROM assessments WHERE subject LIKE ? ORDER BY id", (f"deal:{deal}/%",)).fetchall()
    analyst = [r for r in rows if r["agent"] == "call_analyst"]
    mine = {r["subject"].split("/")[-1]: r for r in rows if r["agent"] == "deal_strategist"}
    assert len(analyst) == 1 and analyst[0]["stance"] == "interest high, urgency unproven"
    assert mine["momentum"]["confidence"] == "high" and mine["urgency"]["confidence"] == "low"

    # reconciliation cites both ids, both stances survive
    rec = db.execute("SELECT * FROM reconciliations WHERE subject=?", (f"deal:{deal}/momentum",)).fetchone()
    assert rec["verdict"] == "Interest high, momentum unproven"
    assert sorted(tables._loads(rec["assessment_ids"], [])) == sorted([analyst[0]["id"], mine["momentum"]["id"]])

    run = db.execute("SELECT * FROM agent_runs WHERE agent='deal_strategist'").fetchone()
    assert run["status"] == "ok" and "health:50" in run["created_items"]


def test_user_set_position_survives_a_weaker_claim(db, fake_llm):
    deal, call, (arjun, me) = run_call(db, fake_llm)
    sid = tables.stakeholder_id(deal, arjun)
    tables.upsert(db, "stakeholders", sid, {}, {"position": "skeptic"}, "user_input", {"kind": "user_input"},
                  actor="user")
    db.commit()
    strategist.run_for_deal(db, deal, force=True)
    assert db.execute("SELECT position FROM stakeholders WHERE id=?", (sid,)).fetchone()[0] == "skeptic"
    conflict = db.execute("SELECT * FROM memory_conflicts WHERE entity_id=? AND field='stakeholders.position' "
                          "AND status='open'", (sid,)).fetchone()
    assert (conflict["existing_value"], conflict["existing_confidence"]) == ("skeptic", "user_input")
    assert conflict["proposed_value"] == "champion"
    prompt = calls_of(fake_llm, "DealStrategy")[-1]["prompt"]
    assert "SET BY MAYA" in prompt and "position=skeptic" in prompt

    # Maya accepts the strategist's read: that settles it as his input
    assert gate.resolve_conflict(db, conflict["id"], accept=True)
    assert db.execute("SELECT position FROM stakeholders WHERE id=?", (sid,)).fetchone()[0] == "champion"


def test_weaker_strategist_claim_does_not_overwrite_a_stronger_one(db, fake_llm):
    deal, call, _ = run_call(db, fake_llm)
    script_intel(fake_llm, eb_status="partial", eb_conf="medium")
    strategist.run_for_deal(db, deal, force=True)
    mid = tables.meddpicc_id(deal, "economic_buyer")
    assert db.execute("SELECT status FROM meddpicc WHERE id=?", (mid,)).fetchone()[0] == "known"
    c = db.execute("SELECT * FROM memory_conflicts WHERE entity_id=? AND field='meddpicc.status'", (mid,)).fetchone()
    assert (c["proposed_value"], c["proposed_confidence"], c["existing_confidence"]) == ("partial", "medium", "high")
    assert any(x["what"] == "Economic Buyer" for x in tables.conflicts(db, deal))


def test_fabricated_quote_lowers_confidence(db, fake_llm):
    deal, call, (arjun, me) = run_call(db, fake_llm, fabricate=True)
    row = db.execute("SELECT * FROM stakeholders WHERE id=?", (tables.stakeholder_id(deal, arjun),)).fetchone()
    assert row["confidence"] == "low"
    assert db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='position'",
                      (row["id"],)).fetchone()[0] == "low"
    evid = tables._loads(row["evidence"], [])
    assert evid[0]["found"] is False and "quote not found in cited turns" in evid[0]["notes"]
    rejected = db.execute("SELECT rejected_items FROM agent_runs WHERE agent='deal_strategist'").fetchone()[0]
    assert "high->low" in rejected


def test_reconciliation_falls_back_to_the_conservative_stance(db, fake_llm):
    deal, call, _ = run_call(db, fake_llm, reconciler=False)
    runs = db.execute("SELECT status FROM agent_runs WHERE agent='assessment_reconciler'").fetchall()
    assert [r["status"] for r in runs] == ["invalid", "invalid"]
    rec = db.execute("SELECT * FROM reconciliations WHERE subject=?", (f"deal:{deal}/momentum",)).fetchone()
    assert rec["verdict"] == "interest high, urgency unproven"           # the analyst's, more conservative
    assert rec["rationale"].startswith("Deterministic fallback") and "Strong momentum" in rec["rationale"]
    kept = db.execute("SELECT agent, stance FROM assessments WHERE subject=?", (f"deal:{deal}/momentum",)).fetchall()
    assert {r["agent"] for r in kept} == {"call_analyst", "deal_strategist"}


def test_weekday_mismatches_are_flagged():
    today = "2026-09-12"                                                 # a Saturday
    assert strategist.weekday_mismatches("ask him to confirm by Monday Sep 15.", today) == \
        ["says 'Monday Sep 15', but 2026-09-15 is a Tuesday"]
    assert strategist.weekday_mismatches("Tuesday, 15th September works", today) == []
    assert strategist.weekday_mismatches("Saturday 9 Jan", today) == []          # next January: 2027-01-09
    assert strategist.weekday_mismatches("Monday Feb 30 and Sunday Sep 13", today) == []
    assert strategist.weekday_mismatches("", today) == []


def test_level_classifier():
    assert reconcile.classify("No urgency. No compelling event.") == "none"
    assert reconcile.classify("Moderate forward movement but fragile") == "weak"
    assert reconcile.classify("Interest is high. Buying intent is present but conditional") == "moderate"
    assert reconcile.classify("Strong buying signal") == "strong"
    assert reconcile.classify("") == "unknown"


def test_pipeline_survives_a_failing_strategist(db, fake_llm):
    deal, people = _setup(db)
    _script(fake_llm)                          # no DealStrategy response: the strategist fails twice
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    assert repo.get_call(db, call)["wf_state"] == "awaiting_review"
    assert db.execute("SELECT COUNT(*) FROM emails WHERE call_id=?", (call,)).fetchone()[0] == 1
    failed, _ = tables.latest_strategy(db, deal, include_failed=True)
    assert "deal_strategist failed" in failed["failed"]
    assert db.execute("SELECT COUNT(*) FROM stakeholders").fetchone()[0] == 0


def test_rerun_with_unchanged_inputs_hits_the_cache(db, fake_llm):
    deal, call, _ = run_call(db, fake_llm)
    strategist.run_for_call(db, call)          # the CFO it created now appears in the prompt: one more run
    before = len(fake_llm.calls)
    again = strategist.run_for_call(db, call)
    assert len(fake_llm.calls) == before and again["deal_health"]["score"] == 50
    # the core re-analysing the call deletes every assessment of it; a cache hit writes them back
    db.execute("DELETE FROM assessments WHERE call_id=?", (call,))
    db.commit()
    strategist.run_for_call(db, call)
    assert db.execute("SELECT COUNT(*) FROM assessments WHERE call_id=? AND agent='deal_strategist'",
                      (call,)).fetchone()[0] == 2


def _limited(system, prompt):
    raise RateLimited("You've hit your session limit")


def test_rate_limited_strategist_defers_the_step_and_resumes(db, fake_llm, monkeypatch):
    monkeypatch.setattr(bus, "RETRY_BACKOFF_S", 0)
    deal, people = _setup(db)
    _script(fake_llm)
    script_intel(fake_llm)
    fake_llm.responses["DealStrategy"] = _limited
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    row = repo.get_call(db, call)
    assert row["wf_state"] == "loops_reconciled" and "rate limited" in row["wf_error"]
    ev_row = db.execute("SELECT status, attempts FROM wf_events WHERE entity_id=? AND error LIKE '%rate limited%'",
                        (call,)).fetchone()
    assert (ev_row["status"], ev_row["attempts"]) == ("pending", 0)
    assert db.execute("SELECT COUNT(*) FROM emails WHERE call_id=?", (call,)).fetchone()[0] == 0
    assert len(calls_of(fake_llm, "DealStrategy")) == 1                  # no retry burst, no fallback

    script_intel(fake_llm)                                               # the quota is back
    db.execute("UPDATE wf_events SET updated_at='2000-01-01T00:00:00+00:00', not_before=NULL WHERE status='pending'")
    db.commit()
    worker.drain(db)
    assert repo.get_call(db, call)["wf_state"] == "awaiting_review"
    assert tables.health(db, deal)["score"] == 50
    assert db.execute("SELECT COUNT(*) FROM emails WHERE call_id=?", (call,)).fetchone()[0] == 1


def test_rate_limited_requests_wait_without_spending_attempts(db, fake_llm, capsys):
    deal, call, _ = run_call(db, fake_llm)
    fake_llm.responses["DealStrategy"] = _limited
    fake_llm.responses["CoachReport"] = _limited
    bus.publish(db, Event(type="STRATEGY_REQUESTED", entity_id=deal, payload={"force": True}, dedupe_key="s1"))
    bus.publish(db, Event(type="COACH_REPORT_REQUESTED", entity_id="coach", payload={}, dedupe_key="c1"))
    db.commit()
    worker.drain(db)
    rows = db.execute("SELECT status, attempts, error FROM wf_events WHERE type IN "
                      "('STRATEGY_REQUESTED','COACH_REPORT_REQUESTED')").fetchall()
    assert len(rows) == 2 and all(r["status"] == "pending" and r["attempts"] == 0 and "rate limited" in r["error"]
                                  for r in rows)
    assert tables.health(db, deal)["score"] == 50                        # the stored read is untouched
    failed, _ = tables.latest_strategy(db, deal, include_failed=True)
    assert failed["rate_limited"] is True
    assert intelligence.cmd_strategy(argparse.Namespace(deal=deal, force=True, json=False)) == 1
    assert "quota is exhausted" in capsys.readouterr().out


def test_cli_strategy(db, fake_llm, capsys):
    deal, call, _ = run_call(db, fake_llm)
    from salescoach.cli import main
    main(["strategy", "--deal", deal])
    out = capsys.readouterr().out
    assert "NEXT BEST ACTION" in out and NBA in out and "DEAL HEALTH 50/100" in out and "Economic Buyer" in out
