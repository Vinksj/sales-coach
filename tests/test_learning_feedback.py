"""Learning layer, phase F2: learned patterns fed back into behaviour, plus the three fixes.

Offline. Every model is the FakeProvider (`fake_llm`); nothing here can reach a real one. The
intel_env fixture (imported from the strategist tests, autouse here) swaps the embedder for a fake,
so a prep brief never looks for Ollama.
"""
import argparse
import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from salescoach import config, learning, repo
from salescoach.agents.email_drafter import EmailAgent
from salescoach.automation import followup
from salescoach.coach import settings as coach_settings
from salescoach.intel import prep, strategist, tables
from salescoach.learning import feedback, outcomes, patterns, weekly
from salescoach.orchestrator import worker
from salescoach.plugins import learning as plugin
from salescoach.sources import paste
from salescoach.store.stores import now
from salescoach.web.app import create_app
from salescoach.store.db import insert_id
from test_core_pipeline import CALL1, _script, _setup
from test_learning_support import day, live_nudge, make_call, make_deal, pattern, sent_edit, tag
from test_p2_engine import QUIET, call, run_engine, slow_output  # noqa: F401  (call is a fixture)
from test_p3_strategy import calls_of, intel_env, run_call, script_intel  # noqa: F401  (intel_env: autouse fixture)
from test_p4_followup import decision, nudge
from test_p4_support import TODAY, cfg, make_loop, world  # noqa: F401  (cfg and world are fixtures)

ORIGIN = {"origin": "http://127.0.0.1:8140"}
SECRET = "Asha said the Bhiwandi detention bill is eleven percent of their freight spend"
LONG = "Hi Asha,\n\n" + SECRET + ". " + " ".join(["word"] * 60) + "\n\nThanks,\nS"
SHORT = "Hi Asha,\n\n" + SECRET + ".\n\nThanks,\nS"
EDITS = {
    "shorten": (LONG, SHORT),
    "no_exclamation": ("Hi A,\n\nGreat news!\n\nThanks,\nS", "Hi A,\n\nGood news.\n\nThanks,\nS"),
    "drop_greeting": ("Dear Arjun,\n\nHello there friend", "Arjun,\n\nHello there friend"),
    "remove_phrase:kindly": ("Hi A,\n\nKindly revert.\n\nThanks,\nS", "Hi A,\n\nPlease reply.\n\nThanks,\nS"),
}
SHORTEN = patterns.pattern_id("email_voice", "shorten")


def voice_rule(db, deal, key, n=3, start=1):
    for i in range(n):
        sent_edit(db, deal, *EDITS[key], sent_at=day(start + i))
    db.commit()


def seller_calls(db, plan):
    """plan: [(deal_id, n_day, {tag: polarity})] -> call ids; several tags may sit on one call."""
    ids = []
    for deal_id, n, tags in plan:
        cid = make_call(db, deal_id, n)
        for name, polarity in tags.items():
            tag(db, cid, name, polarity=polarity)
        ids.append(cid)
    db.commit()
    return ids


def email_ctx(db, deal, learned):
    """What step_email hands the email agent, for a real (pasted) call on the deal."""
    from salescoach.orchestrator import context
    call_id = repo.create_call(db, source="paste", title="Weekly", deal_id=deal, started_at=day(40), wf_state="complete")
    ctx = context.load(db, call_id)
    ctx.update(allowed_recipients={"buyer@x.test": "Buyer"}, email_actions=[], summary={}, recent_edits=[],
               learned_voice=learned)
    return ctx


def refs_of(db, agent):
    return [json.loads(r["input_refs"]) for r in db.execute("SELECT input_refs FROM agent_runs WHERE agent=? ORDER BY id",
                                                           (agent,))]


# ---- 1a. email and nudge drafters ----------------------------------------------------------------------------

def test_voice_rules_feed_only_when_active_and_only_when_the_flag_is_on(db, cfg):  # noqa: F811
    deal = make_deal(db, "A")
    voice_rule(db, deal, "shorten", n=2)
    patterns.recompute(db)
    assert feedback.select(db, "email_drafter") == [] and feedback.voice_block([]) == ""     # a candidate feeds nothing
    voice_rule(db, deal, "shorten", n=1, start=3)
    patterns.recompute(db)
    for target in ("email_drafter", "nudge_drafter"):
        [got] = feedback.select(db, target)
        assert got["id"] == SHORTEN and got["rule"] == "Keep it shorter than the draft would be."
    block = feedback.voice_block(feedback.select(db, "email_drafter"))
    assert block.splitlines()[0] == "HOW MAYA WRITES (learned from edits)"
    assert f"- [{SHORTEN}] Keep it shorter than the draft would be. (seen in 3+ edits)" in block.splitlines()
    assert feedback.select(db, "prep") == [] and feedback.select(db, "strategist") == []    # other families only

    cfg["learning"] = {"feedback": {"email_drafter": False}}
    assert feedback.select(db, "email_drafter") == [] and len(feedback.select(db, "nudge_drafter")) == 1
    cfg["learning"] = {"feedback": {"nudge_drafter": False, "max_patterns": 0}}
    assert feedback.select(db, "nudge_drafter") == []


def test_cap_of_three_and_do_not_use_in_prompts(db, cfg):  # noqa: F811
    deal = make_deal(db, "A")
    for key in EDITS:
        voice_rule(db, deal, key)
    patterns.recompute(db)
    assert len(patterns.for_prompt(db, "email_drafter", limit=10)) == 4
    got = feedback.ids(feedback.select(db, "email_drafter"))
    assert len(got) == 3 and got == sorted(got)                       # same label and bucket: id order, stable
    cfg["learning"] = {"feedback": {"max_patterns": 9}}
    assert len(feedback.select(db, "email_drafter")) == 3             # the cap cannot be raised past 3
    cfg.pop("learning")

    dropped = got[0]
    row = patterns.set_prompt_use(db, dropped, use=False)
    assert row["no_prompt"] == 1 and row["status"] == "active"        # still believed, still counted, just not fed
    prov = db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='no_prompt'", (dropped,)).fetchone()
    assert prov["confidence"] == "user_input"
    assert dropped not in [p["id"] for p in patterns.for_prompt(db, "email_drafter", limit=10)]
    after = feedback.ids(feedback.select(db, "nudge_drafter"))
    assert dropped not in after and len(after) == 3
    patterns.recompute(db)                                            # a recompute never clears the user's flag
    assert pattern(db, dropped)["no_prompt"] == 1
    patterns.set_prompt_use(db, dropped, use=True)
    assert feedback.ids(feedback.select(db, "email_drafter")) == got


def test_block_is_byte_stable_until_the_active_set_a_label_or_a_bucket_changes(db):
    d1, d2, d3 = (make_deal(db, n) for n in "ABC")
    voice_rule(db, d1, "shorten")
    weak, other = "avoids_budget", "no_decision_process"
    seller_calls(db, [(d1, 0, {weak: "weakness", other: "weakness"}), (d2, 1, {weak: "weakness", other: "weakness"}),
                      (d1, 2, {weak: "weakness", other: "weakness"}), (d2, 3, {other: "weakness"})])
    patterns.recompute(db)
    ctx = email_ctx(db, d3, [])

    def blocks():
        voice_block = feedback.voice_block(feedback.select(db, "email_drafter"))
        lines = "\n".join(feedback.prep_lines(feedback.select(db, "prep")))
        prompt = EmailAgent().build_prompt({**ctx, "learned_voice": feedback.select(db, "email_drafter")})
        return voice_block, lines, hashlib.sha256(prompt.encode()).hexdigest()   # the sha is the step cache key

    before = blocks()
    ranked_before = [p["key"] for p in patterns.for_prompt(db, "prep")]
    assert ranked_before == [other, weak]                              # for_prompt ranks by the exact n: 4 calls, then 3

    # More of the same: two more edits, two more calls with one tag. Exact counts move, order by n flips.
    voice_rule(db, d2, "shorten", n=2, start=10)
    seller_calls(db, [(d1, 4, {weak: "weakness"}), (d2, 5, {weak: "weakness"})])
    patterns.recompute(db)
    assert pattern(db, SHORTEN)["n_obs"] == 5 and pattern(db, patterns.pattern_id("seller", weak))["n_calls"] == 5
    assert [p["key"] for p in patterns.for_prompt(db, "prep")] == [weak, other]
    assert blocks() == before                                          # ...and the prompt does not move a byte
    patterns.recompute(db)
    assert blocks() == before

    # A sixth call on a third deal crosses a bucket and a label: that IS a new belief, and the text says so.
    seller_calls(db, [(d3, 6, {weak: "weakness"})])
    patterns.recompute(db)
    assert pattern(db, patterns.pattern_id("seller", weak))["label"] == "established"
    changed = blocks()
    assert changed[1] != before[1] and "established, seen on 6+ calls across 3+ deals" in changed[1]
    assert changed[0] == before[0]                                     # the email rule did not change, nor did its block
    assert "2026" not in changed[0] + changed[1]                       # no timestamps in a fed block


def test_count_text_is_coarse():
    assert [feedback.count_text(n, "call") for n in (1, 2, 3, 5, 6, 9, 10, 19, 20, 120)] == [
        "1 call", "2 calls", "3+ calls", "3+ calls", "6+ calls", "6+ calls", "10+ calls", "10+ calls", "20+ calls",
        "100+ calls"]


def test_email_pipeline_feeds_the_rule_records_its_id_and_leaks_no_other_customers_text(db, fake_llm):
    other = make_deal(db, "Other customer")
    voice_rule(db, other, "shorten")
    patterns.recompute(db)
    db.commit()
    deal, people = _setup(db)
    _script(fake_llm)
    call_id = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    assert repo.get_call(db, call_id)["wf_state"] == "awaiting_review"
    [draft] = calls_of(fake_llm, "EmailDraft")
    assert f"[{SHORTEN}] Keep it shorter than the draft would be." in draft["prompt"]
    assert "HOW MAYA WRITES (learned from edits)" in draft["prompt"]
    for needle in (SECRET, "Bhiwandi", "eleven percent", "Asha"):
        assert needle not in draft["prompt"] and needle not in draft["system"], needle
    assert "HOW MAYA EDITED EARLIER DRAFTS (learn from these)\n(none yet)" in draft["prompt"]   # this deal has none
    assert refs_of(db, "email")[-1] == {"call_id": call_id, "patterns": [SHORTEN]}

    # The same deal's own raw edits still arrive as before, next to the rules.
    ctx = email_ctx(db, other, feedback.select(db, "email_drafter", other))
    ctx["recent_edits"] = [{"draft_body": LONG, "final_body": SHORT}]
    assert SECRET in EmailAgent().build_prompt(ctx)


def test_without_learned_rules_the_email_prompt_is_exactly_what_it_was(db):
    ctx = email_ctx(db, make_deal(db, "A"), [])
    plain = dict(ctx)
    plain.pop("learned_voice")
    assert EmailAgent().build_prompt(ctx) == EmailAgent().build_prompt(plain)
    assert "HOW MAYA WRITES" not in EmailAgent().build_prompt(ctx)
    assert EmailAgent().input_refs(ctx) == {"call_id": ctx["call_id"], "patterns": []}


def test_nudge_prompt_gets_the_rules_and_the_run_records_them(db, world, fake_llm, cfg):  # noqa: F811
    voice_rule(db, make_deal(db, "Other customer"), "shorten")
    patterns.recompute(db)
    db.commit()
    make_loop(db, world.deal)
    fake_llm.responses.update({"FollowupDecision": decision(), "NudgeDraft": nudge()})
    [r] = followup.evaluate_due(db, TODAY)
    assert r["decision"] == "send_nudge"
    draft = calls_of(fake_llm, "NudgeDraft")[-1]
    assert draft["prompt"].rstrip().endswith("(seen in 3+ edits)") and f"[{SHORTEN}]" in draft["prompt"]
    assert SECRET not in draft["prompt"] and "Bhiwandi" not in draft["prompt"]
    assert refs_of(db, "nudge")[-1]["patterns"] == [SHORTEN]

    cfg["learning"] = {"feedback": {"nudge_drafter": False}}
    db.execute("UPDATE emails SET status='rejected'")
    make_loop(db, world.deal, "Anita to confirm the budget owner")
    followup.evaluate_due(db, TODAY)
    off = calls_of(fake_llm, "NudgeDraft")[-1]
    assert "HOW MAYA WRITES" not in off["prompt"] and refs_of(db, "nudge")[-1]["patterns"] == []


# ---- 1b. prep writer -----------------------------------------------------------------------------------------------

def _prep_world(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    seller_calls(db, [(d1, n, {"avoids_budget": "weakness", "no_decision_process": "weakness",
                               "leaves_open_loops": "weakness", "quantifies_impact": "strength",
                               "multi_threads": "strength"}) for n in (0, 2)]
                 + [(d2, 1, {"avoids_budget": "weakness", "no_decision_process": "weakness",
                             "leaves_open_loops": "weakness", "quantifies_impact": "strength", "multi_threads": "strength"})])
    return d1, d2


def test_prep_gets_weaknesses_first_then_one_strength_and_falls_back_to_the_old_path(db, fake_llm, cfg):  # noqa: F811
    from salescoach.memory import patterns as seller_memory
    d1, _ = _prep_world(db)
    seller_memory.recompute(db)                                        # the old seller_patterns path
    db.commit()
    old = prep.render_facts(prep.assemble(db, d1))
    assert "COACHING PRIORITY: " in old and "LEARNED SELLING PATTERNS" not in old     # nothing learned yet: as before
    assert prep.assemble(db, d1)["learned"] == []

    patterns.recompute(db)
    db.commit()
    brief = prep.assemble(db, d1)
    assert [(p["polarity"], p["key"]) for p in brief["learned"]] == [
        ("weakness", "avoids_budget"), ("weakness", "leaves_open_loops"), ("strength", "multi_threads")]
    text = prep.render_facts(brief)
    assert "COACHING PRIORITY: Avoiding the budget conversation | Ask who owns the budget" in text
    line = next(ln for ln in text.splitlines() if "lp:seller:global:avoids_budget" in ln)
    assert line.startswith("- [lp:seller:global:avoids_budget] weakness, emerging, seen on 3+ calls across 2 deals: ")
    assert text.index("leaves_open_loops") < text.index("multi_threads") and "quantifies_impact" not in text
    assert text.splitlines()[[i for i, ln in enumerate(text.splitlines()) if "multi_threads" in ln][0]].endswith(
        "Multi-threads beyond the champion")                           # a strength carries no "do:" instruction

    fake_llm.responses["PrepWriting"] = {"objective": "o", "objective_why": "w", "opening": "hi", "close": "bye",
                                         "watch_for": []}
    prep.generate(db, d1)
    assert calls_of(fake_llm, "PrepWriting")[-1]["prompt"] == text
    assert refs_of(db, "prep_writer")[-1] == {"deal_id": d1, "patterns": [p["id"] for p in brief["learned"]]}

    cfg["learning"] = {"feedback": {"prep": False}}
    assert prep.render_facts(prep.assemble(db, d1)) == old             # flag off: the old behaviour, byte for byte


# ---- 1c. live coach ----------------------------------------------------------------------------------------------------

def test_live_boost_is_bounded_and_loses_to_the_users_own_weight(seller_settings, monkeypatch):
    base = coach_settings.load()["scoring"]["weights"]
    assert (base["quantify_impact"], base["objection"]) == (0.75, 1.0)
    got = coach_settings.load(learned={"quantify_impact": 0.1, "objection": 0.1, "no_such_trigger": 0.5})["scoring"]["weights"]
    assert got["quantify_impact"] == 0.85 and got["objection"] == 1.0 and "no_such_trigger" not in got
    assert coach_settings.boosted(0.95, 0.3) == 1.0 and coach_settings.boosted(0.5, -1) == 0.5

    # After the methodology's weights...
    monkeypatch.setattr(coach_settings, "methodology_weights", lambda: {"quantify_impact": 0.9})
    assert coach_settings.load(learned={"quantify_impact": 0.1})["scoring"]["weights"]["quantify_impact"] == 1.0
    # ...and before the user's: an explicit setting always wins, a run override wins over that.
    (seller_settings / "live_coach.yaml").write_text(yaml.safe_dump({"scoring": {"weights": {"quantify_impact": 0.4}}}))
    assert coach_settings.load(learned={"quantify_impact": 0.1})["scoring"]["weights"]["quantify_impact"] == 0.4
    over = {"scoring": {"weights": {"quantify_impact": 0.2}}}
    assert coach_settings.load(over, learned={"quantify_impact": 0.1})["scoring"]["weights"]["quantify_impact"] == 0.2


def test_live_focus_is_the_top_mapped_weakness_and_unmapped_tags_are_skipped(db, cfg):  # noqa: F811
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    plan = {"avoids_budget": "weakness", "quantifies_impact": "strength"}           # neither maps to a live trigger
    seller_calls(db, [(d1, 0, plan), (d2, 1, plan), (d1, 2, plan)])
    patterns.recompute(db)
    assert feedback.live_focus(db) is None and feedback.select(db, "live_coach") == []
    seller_calls(db, [(d, 3 + i, {"no_impact_quantification": "weakness"}) for i, d in enumerate((d1, d2, d1))])
    patterns.recompute(db)
    focus = feedback.live_focus(db)
    assert (focus["trigger"], focus["boost"], focus["pattern"]["key"]) == ("quantify_impact", 0.1, "no_impact_quantification")
    assert feedback.live_triggers()["no_follow_up_questions"] == "dig_deeper"

    cfg["learning"] = {"feedback": {"live_coach": {"boost": 5}, "live_triggers": {"avoids_budget": "not_a_trigger"}}}
    assert feedback.boost() == feedback.MAX_BOOST and "avoids_budget" not in feedback.live_triggers()
    cfg["learning"] = {"feedback": {"live_coach": {"enabled": False}}}
    assert feedback.live_focus(db) is None
    cfg["learning"] = {"feedback": {"live_coach": False}}                 # the plain true|false form works too
    assert feedback.live_focus(db) is None


def test_live_coach_gets_the_boost_the_habit_line_and_records_the_id(db, call, fake_llm):  # noqa: F811
    fake_llm.responses["SlowPassOutput"] = lambda system, prompt: slow_output(interventions=[])
    slow = {"slow": {"interval_s": 10, "min_new_segments": 1, "max_passes": 1}}
    plain, *_ = run_engine(call, QUIET, slow=True, overrides=slow)
    assert plain.focus is None and plain.cfg["scoring"]["weights"]["quantify_impact"] == 0.75
    before = calls_of(fake_llm, "SlowPassOutput")[-1]["system"]
    assert "Priority habit" not in before and refs_of(db, "live_coach")[-1]["patterns"] == []

    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    seller_calls(db, [(d, i, {"no_impact_quantification": "weakness"}) for i, d in enumerate((d1, d2, d1))])
    patterns.recompute(db)
    db.commit()
    pid = patterns.pattern_id("seller", "no_impact_quantification")
    engine, *_ = run_engine(call, QUIET, slow=True, overrides=slow)
    assert engine.cfg["scoring"]["weights"]["quantify_impact"] == 0.85 and engine.ranker.weights["quantify_impact"] == 0.85
    system = calls_of(fake_llm, "SlowPassOutput")[-1]["system"]
    assert system.startswith(before.rstrip("\n")) and system.count("Priority habit") == 1
    habit = system.rstrip("\n").splitlines()[-1]
    assert habit == (f"Priority habit, learned across Maya's past calls [{pid}]: \"Not quantifying impact\" "
                     "(emerging, seen on 3+ calls across 2 deals). When it is a close call between two nudges, prefer "
                     "quantify_impact. It is a habit to watch for, not evidence about this call.")
    assert refs_of(db, "live_coach")[-1]["patterns"] == [pid]


# ---- 1d. strategist ----------------------------------------------------------------------------------------------------------

def test_strategist_priors_absent_until_promoted_labelled_and_never_evidence(db, fake_llm, cfg):  # noqa: F811
    deal, call_id, (arjun, _me) = run_call(db, fake_llm)
    db.execute("UPDATE people SET title='CFO' WHERE node_id=?", (arjun,))
    patterns.recompute(db)
    db.commit()
    finance = patterns.pattern_id("persona", "finance")
    assert pattern(db, finance)["status"] == "candidate"                  # observed, not promoted (1 deal of 8)
    strategist.run_for_call(db, call_id, force=True)
    first = calls_of(fake_llm, "DealStrategy")[-1]["prompt"]
    assert "PRIORS FROM PAST DEALS" not in first and refs_of(db, "deal_strategist")[-1]["patterns"] == []

    cfg["learning"] = {"observation_only": {"min_deals": 1, "min_per_bucket": 1}}
    patterns.recompute(db)
    db.commit()
    assert pattern(db, finance)["status"] == "active"
    # The model now tries to use the prior as its evidence for the economic buyer.
    script_intel(fake_llm, eb_quote="Finance stakeholders")
    strategist.run_for_call(db, call_id, force=True)
    prompt = calls_of(fake_llm, "DealStrategy")[-1]["prompt"]
    block = prompt[prompt.index("PRIORS FROM PAST DEALS"):prompt.index("CALL HISTORY (oldest first)")]
    assert prompt.replace(block, "") == first                            # the block is the ONLY thing that was added
    assert block.splitlines()[0] == "PRIORS FROM PAST DEALS (NOT evidence about this deal)"
    assert "A prior is never evidence" in block and f"- [{finance}] Finance stakeholders (seen on 1 deal)" in block
    assert refs_of(db, "deal_strategist")[-1] == {"call_id": call_id, "deal_id": deal, "methodology": "meddpicc",
                                                  "patterns": [finance]}
    # The evidence validator is untouched: 'known' still needs a quote found in this deal's turns.
    stored, _ = tables.latest_strategy(db, deal)
    eb = next(m for m in stored["meddpicc"] if m["element"] == "economic_buyer")
    assert eb["status"] == "unknown" and eb["evidence"][0]["found"] is False
    assert any("needs a verified quote" in n for n in eb["validation_notes"])

    assert feedback.select(db, "strategist", deal_id="some-other-deal") == []   # a bucket never seen on that deal
    cfg["learning"] = {"observation_only": {"min_deals": 1, "min_per_bucket": 1}, "feedback": {"strategist": False}}
    assert feedback.select(db, "strategist", deal) == []


# ---- 2. "Used in" and the toggle on the page ---------------------------------------------------------------------------

def test_used_in_counts_runs_and_the_page_toggles_prompt_use(db):
    deal = make_deal(db, "A")
    voice_rule(db, deal, "shorten")
    patterns.recompute(db)
    for agent, n in (("email", 2), ("nudge", 1), ("summary", 1)):
        for _ in range(n):
            db.execute("INSERT INTO agent_runs(agent,input_refs,status,started_at) VALUES (?,?,'ok',?)",
                       (agent, json.dumps({"patterns": [SHORTEN]} if agent != "summary" else {"call_id": "x"}), now()))
    db.commit()
    used = feedback.used_in(db)[SHORTEN]
    assert used == {"targets": ["email_drafter", "nudge_drafter"], "runs": 3,
                    "by_target": {"email_drafter": 2, "nudge_drafter": 1}}

    client = TestClient(create_app(start_worker=False, live_factory=None))
    page = client.get("/learning").text
    assert "Used in:" in page and "follow-up email" in page and "seen by <b>3</b> agent runs" in page
    assert "Do not use in prompts" in page and "What changed in the last 7 days" in page
    assert client.post("/learning/patterns", data={"id": SHORTEN, "action": "no_prompt"}).status_code == 403   # no origin
    r = client.post("/learning/patterns", data={"id": SHORTEN, "action": "no_prompt"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert pattern(db, SHORTEN)["no_prompt"] == 1 and feedback.select(db, "email_drafter") == []
    page = client.get("/learning").text
    assert "you: not used in prompts" in page and "Use in prompts again" in page
    client.post("/learning/patterns", data={"id": SHORTEN, "action": "use_prompt"}, headers=ORIGIN)
    assert pattern(db, SHORTEN)["no_prompt"] == 0 and len(feedback.select(db, "email_drafter")) == 1


# ---- Fix A: a methodology switch is not progress ----------------------------------------------------------------------------

def _strategist_run(db, call_id, deal, methodology_key):
    refs = {"call_id": call_id, "deal_id": deal}
    if methodology_key:
        refs["methodology"] = methodology_key
    return insert_id(db.execute("INSERT INTO agent_runs(agent,call_id,input_refs,status,started_at) VALUES "
                      "('deal_strategist',?,?,'ok',?)", (call_id, json.dumps(refs), now())))


def _element(db, deal, element, status, run, call_id):
    tables.upsert(db, "meddpicc", tables.meddpicc_id(deal, element), {"deal_id": deal, "element": element},
                  {"status": status}, "high", {"kind": "strategist", "ref": f"run:{run}", "call": call_id})


def _advanced(db, call_id):
    row = db.execute("SELECT value, details FROM derived_outcomes WHERE kind='call_advanced' AND subject_id=?",
                     (call_id,)).fetchone()
    return row["value"], json.loads(row["details"])


def test_rows_created_by_the_first_run_after_a_methodology_switch_are_not_an_advance(db):
    deal = make_deal(db, "NWP")
    c1, c2, c3 = (make_call(db, deal, n) for n in (0, 7, 14))
    r1 = _strategist_run(db, c1, deal, None)                           # a run from before methodologies: MEDDPICC
    _element(db, deal, "economic_buyer", "unknown", r1, c1)
    r2 = _strategist_run(db, c2, deal, "bant")                         # the switch: BANT's rows appear, already "known"
    _element(db, deal, "budget", "known", r2, c2)
    _element(db, deal, "need", "partial", r2, c2)
    _element(db, deal, "authority", "unknown", r2, c2)
    r3 = _strategist_run(db, c3, deal, "bant")
    _element(db, deal, "authority", "known", r3, c3)                   # a status change: counts whatever happened before
    outcomes.recompute(db, today=date(2026, 9, 15))
    value, details = _advanced(db, c2)
    assert value == 0 and details["elements_now_known"] == []
    assert details["elements_created_not_counted"] == ["budget", "need"]
    value, details = _advanced(db, c3)
    assert value == 1 and details["elements_now_known"] == [{"element": "authority", "to": "known"}]
    assert "elements_created_not_counted" not in details


def test_a_row_created_under_the_same_methodology_is_an_advance(db):
    deal = make_deal(db, "NWP")
    c1, c2 = make_call(db, deal, 0), make_call(db, deal, 7)
    r1 = _strategist_run(db, c1, deal, "bant")
    _element(db, deal, "budget", "unknown", r1, c1)
    r2 = _strategist_run(db, c2, deal, "bant")                         # same framework: `timeline` is new to the DEAL
    _element(db, deal, "timeline", "known", r2, c2)
    _element(db, deal, "budget", "known", r2, c2)                      # and a row that was there went unknown -> known
    outcomes.recompute(db, today=date(2026, 9, 15))
    value, details = _advanced(db, c2)
    assert value == 1
    assert sorted(e["element"] for e in details["elements_now_known"]) == ["budget", "timeline"]

    # The deal's very first strategist run has no earlier read to have advanced from.
    other = make_deal(db, "OM")
    o1, o2 = make_call(db, other, 0), make_call(db, other, 7)
    first = _strategist_run(db, o2, other, "bant")
    _element(db, other, "budget", "known", first, o2)
    outcomes.recompute(db, today=date(2026, 9, 15))
    assert _advanced(db, o2)[0] == 0 and o1


# ---- Fix B: proposals are relative to the weight in force ----------------------------------------------------------------------

def test_trigger_proposal_uses_the_resolved_weight_and_accepting_writes_the_overlay(db, seller_settings, monkeypatch):
    # The user already runs root_cause at 0.5 and the methodology asks for dig_deeper at 0.9.
    (seller_settings / "live_coach.yaml").write_text(yaml.safe_dump(
        {"budget": {"cooldown_s": 75}, "scoring": {"weights": {"root_cause": 0.5}}}))
    monkeypatch.setattr(coach_settings, "methodology_weights", lambda: {"dig_deeper": 0.9})
    assert patterns.resolved_weights()["root_cause"] == 0.5 and patterns.resolved_weights()["dig_deeper"] == 0.9
    deal = make_deal(db, "A")
    call_id = make_call(db, deal, 0, source="capture")
    for trigger in ("root_cause", "dig_deeper"):
        for _ in range(15):
            live_nudge(db, call_id, trigger, outcome="ignored")
    patterns.recompute(db)
    props = {p["payload"]["trigger"]: p for p in patterns.open_proposals(db)}
    assert (props["root_cause"]["payload"]["current_weight"], props["root_cause"]["payload"]["proposed_weight"]) == (0.5, 0.4)
    assert (props["dig_deeper"]["payload"]["current_weight"], props["dig_deeper"]["payload"]["proposed_weight"]) == (0.9, 0.72)
    assert "from 0.5 to 0.4" in props["root_cause"]["summary"]

    tracked = (config.CONFIG_DIR / "live_coach.yaml").read_text()
    assert patterns.resolve_proposal(db, props["dig_deeper"]["id"], accept=True)["applied"] == "config"
    saved = yaml.safe_load((seller_settings / "live_coach.yaml").read_text())
    assert saved == {"budget": {"cooldown_s": 75}, "scoring": {"weights": {"root_cause": 0.5, "dig_deeper": 0.72}}}
    assert (config.CONFIG_DIR / "live_coach.yaml").read_text() == tracked            # the tracked file is never edited
    assert coach_settings.load()["scoring"]["weights"]["dig_deeper"] == 0.72          # and the live coach now runs on it
    assert "getattr(config" not in open(patterns.__file__).read()                     # no "settings layer missing" fallback


# ---- Fix C: re-proposal once the evidence has materially grown --------------------------------------------------------------------

def _shown(db, call_id, trigger, n, outcome="ignored"):
    for _ in range(n):
        live_nudge(db, call_id, trigger, outcome=outcome)
    db.execute("UPDATE nudges SET shown_wall=? WHERE shown_wall IS NULL", (now(),))
    patterns.recompute(db)


def _later(db):
    """Age every decision by a day, so nudges inserted next count as shown after it."""
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    db.execute("UPDATE learning_proposals SET resolved_at=? WHERE resolved_at IS NOT NULL", (past,))
    db.execute("UPDATE nudges SET shown_wall=?", ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds"),))
    db.execute("UPDATE pattern_observations SET observed_at=? WHERE family='nudge_trigger'",
               ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds"),))


def test_a_dismissed_weight_proposal_returns_only_at_twice_the_evidence_and_keeps_its_history(db):
    deal = make_deal(db, "A")
    call_id = make_call(db, deal, 0, source="capture")
    _shown(db, call_id, "status_quo", 15)
    [first] = patterns.open_proposals(db)
    assert first["payload"]["n"] == 15 and first["payload"]["round"] == 1
    patterns.resolve_proposal(db, first["id"], accept=False)
    assert db.execute("SELECT decided_n FROM learning_proposals WHERE id=?", (first["id"],)).fetchone()[0] == 15
    _later(db)

    _shown(db, call_id, "status_quo", 14)                               # n=29: not yet twice 15
    assert patterns.open_proposals(db) == []
    _shown(db, call_id, "status_quo", 1)                                # n=30 and still ignored: it comes back
    [again] = patterns.open_proposals(db)
    assert again["id"] != first["id"] and again["payload"]["round"] == 2
    assert again["payload"]["previous_proposal"] == first["id"] and again["payload"]["n_at_previous_decision"] == 15
    assert "Raised again: the evidence grew from n=15 to n=30" in again["summary"]
    history = patterns.decided_proposals(db)
    assert [(h["id"], h["status"], h["decided_n"]) for h in history] == [(first["id"], "dismissed", 15)]
    patterns.recompute(db)
    assert len(patterns.open_proposals(db)) == 1                         # one open per subject, however often it runs
    db.commit()
    page = TestClient(create_app(start_worker=False, live_factory=None)).get("/learning").text
    assert "1 decided earlier" in page and "n=15 when you decided" in page and "Raised again" in page


def test_no_reproposal_when_the_condition_stopped_holding_after_the_decision(db, seller_settings):
    deal = make_deal(db, "A")
    call_id = make_call(db, deal, 0, source="capture")
    _shown(db, call_id, "status_quo", 15)
    [first] = patterns.open_proposals(db)
    patterns.resolve_proposal(db, first["id"], accept=True)              # 0.6 -> 0.48 in the overlay
    assert config.load_user("live_coach")["scoring"]["weights"]["status_quo"] == 0.48
    _later(db)
    _shown(db, call_id, "status_quo", 9)
    _shown(db, call_id, "status_quo", 6, outcome="followed")
    # n=30 is twice 15; 24 of 30 were unhelpful overall and 9 of the 15 shown since the decision (exactly the 60%
    # line), so it comes back, and it is relative to the weight now in force (the accepted 0.48).
    [again] = patterns.open_proposals(db)
    assert (again["payload"]["current_weight"], again["payload"]["proposed_weight"]) == (0.48, 0.38)
    patterns.resolve_proposal(db, again["id"], accept=False)
    _later(db)
    _shown(db, call_id, "status_quo", 30, outcome="followed")            # n=60, but followed ever since
    assert patterns.open_proposals(db) == []
    assert [h["status"] for h in patterns.decided_proposals(db)] == ["dismissed", "accepted"]


def test_a_dismissed_merge_returns_when_the_tag_has_been_seen_twice_as_often(db):
    d1 = make_deal(db, "A")
    seller_calls(db, [(d1, 0, {"new:talks_over_the_buyer": "weakness"}), (d1, 1, {"new:talks_over_the_buyer": "weakness"}),
                      (d1, 2, {"new:talks_over_the_buyer": "weakness"}), (d1, 3, {"new:talking_over_buyer": "weakness"}),
                      (d1, 4, {"new:talking_over_buyer": "weakness"})])
    patterns.recompute(db)
    [prop] = patterns.open_proposals(db)
    assert prop["payload"]["from"] == "new:talking_over_buyer" and prop["payload"]["n"] == 2
    patterns.resolve_proposal(db, prop["id"], accept=False)
    seller_calls(db, [(d1, 5, {"new:talking_over_buyer": "weakness"})])
    patterns.recompute(db)
    assert patterns.open_proposals(db) == []                             # 3 < 2 x 2
    seller_calls(db, [(d1, 6, {"new:talks_over_the_buyer": "weakness"}), (d1, 7, {"new:talks_over_the_buyer": "weakness"}),
                      (d1, 8, {"new:talking_over_buyer": "weakness"})])
    patterns.recompute(db)
    [again] = patterns.open_proposals(db)
    assert again["payload"]["from"] == "new:talking_over_buyer" and again["payload"]["n"] == 4


@pytest.mark.sqlite_only          # rebuilds a table an old SQLite install made; the Postgres baseline never had that shape
def test_a_proposals_table_from_phase_f1_is_rebuilt_with_its_history(db):
    db.execute("DROP TABLE learning_proposals")
    db.execute("CREATE TABLE learning_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL CHECK(kind IN "
               "('merge','trigger_weight')), subject TEXT NOT NULL, pattern_id TEXT, target_id TEXT, summary TEXT NOT NULL, "
               "payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'open' CHECK(status IN "
               "('open','accepted','dismissed')), applied TEXT, created_at TEXT NOT NULL, resolved_at TEXT, "
               "UNIQUE(kind, subject))")
    db.execute("INSERT INTO learning_proposals(kind,subject,pattern_id,summary,payload,status,created_at,resolved_at) VALUES "
               "('trigger_weight','trigger:status_quo','lp:nudge_trigger:global:status_quo','old','{\"shown\": 15}',"
               "'dismissed',?,?)", (day(0), day(1)))
    db.commit()
    learning.ensure_columns(db)
    learning.ensure_columns(db)                                          # idempotent
    [row] = db.execute("SELECT * FROM learning_proposals").fetchall()
    assert (row["summary"], row["status"], row["decided_n"]) == ("old", "dismissed", None)
    assert "UNIQUE(kind, subject)" not in db.execute("SELECT sql FROM sqlite_master WHERE name='learning_proposals'").fetchone()[0]

    # A decision from before n was recorded: today's evidence becomes the baseline, nothing is re-raised at once.
    deal = make_deal(db, "A")
    call_id = make_call(db, deal, 0, source="capture")
    _shown(db, call_id, "status_quo", 40)
    assert patterns.open_proposals(db) == []
    assert db.execute("SELECT decided_n FROM learning_proposals WHERE id=?", (row["id"],)).fetchone()[0] == 40


# ---- 6. the weekly digest -------------------------------------------------------------------------------------------------------

def test_digest_counts_what_changed_and_quotes_nothing(db, capsys):
    d1, d2 = make_deal(db, "Northwind"), make_deal(db, "Eastline Logistics")
    quiet = datetime.now(timezone.utc) - timedelta(days=30)
    assert weekly.digest(db, quiet)["empty"] and "Nothing changed." in weekly.digest(db, quiet)["text"]

    seller_calls(db, [(d1, 0, {"avoids_budget": "weakness"}), (d2, 1, {"avoids_budget": "weakness"}),
                      (d1, 2, {"avoids_budget": "weakness"})])
    voice_rule(db, d1, "shorten")                                        # edits that carry a customer's sentence
    live = make_call(db, d1, 4, source="capture")
    for _ in range(15):
        live_nudge(db, live, "dig_deeper", outcome="ignored")
    outcomes.set_deal_outcome(db, d2, {"status": "lost", "lost_reason": outcomes.format_lost_reason("timing", "next FY")},
                              confirmed=True)
    outcomes.set_deal_outcome(db, d1, {"status": "won"}, confirmed=True)
    assert "error" not in plugin.run_recompute(db, trigger="test")
    events = db.execute("SELECT COUNT(*) FROM events WHERE kind='learned_pattern_status'").fetchone()[0]
    plugin.run_recompute(db, trigger="test")                             # a recompute that changes nothing emits nothing
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind='learned_pattern_status'").fetchone()[0] == events

    d = learning.digest(db, quiet)
    assert d == plugin.digest(db, quiet) | {"until": d["until"]}
    assert sorted(p["id"] for p in d["new_active"]) == sorted([patterns.pattern_id("seller", "avoids_budget"), SHORTEN,
                                                               patterns.pattern_id("nudge_trigger", "dig_deeper")])
    assert d["dormant"] == [] and d["returned"] == [] and not d["empty"]
    assert [p["kind"] for p in d["open_proposals"]] == ["trigger_weight"]
    assert {(x["name"], x["status"], x["reason"], x["reason_text"]) for x in d["deals"]} == {
        ("Eastline Logistics", "lost", "Wrong timing", "next FY"), ("Northwind", "won", "", "")}
    replied = next(o for o in d["outcomes"] if o["kind"] == "email_replied")
    assert replied["yes"] + replied["no"] + replied["pending"] == 3 and replied["total"] == 3
    text = d["text"]
    for line in ("New active patterns (3)", "How you sell: Avoiding the budget conversation [emerging]",
                 "Open proposals (1)", "Deals closed (2)", "- Eastline Logistics: lost (Wrong timing: next FY)", "- Northwind: won",
                 "Outcomes that moved"):
        assert line in text, line
    for needle in (SECRET, "Bhiwandi", "eleven percent", "Asha", "a quote from the call"):
        assert needle not in text and needle not in json.dumps(d), needle

    # Dormant and returned are read from the same status events.
    seller_calls(db, [(d2, n, {}) for n in range(5, 15)])
    patterns.recompute(db)
    assert [p["id"] for p in weekly.digest(db, quiet)["dormant"]] == [patterns.pattern_id("seller", "avoids_budget")]
    seller_calls(db, [(d1, 15, {"avoids_budget": "weakness"})])
    patterns.recompute(db)
    back = weekly.digest(db, quiet)
    assert [p["id"] for p in back["returned"]] == [patterns.pattern_id("seller", "avoids_budget")] and back["dormant"] == []
    assert weekly.digest(db, datetime.now(timezone.utc) + timedelta(days=1))["new_active"] == []     # outside the window

    db.commit()
    parser = argparse.ArgumentParser()
    plugin.register_cli(parser.add_subparsers())
    args = parser.parse_args(["learn", "--digest", "--days", "3"])
    args.fn(args)
    out = capsys.readouterr().out
    assert out.startswith("What changed in the last 3 days") and "Returned after going quiet (1)" in out
    assert "Derived outcomes" not in out                                  # --digest alone prints only the digest


def test_nothing_in_the_learning_layer_sends_the_digest():
    import salescoach.learning.weekly as module
    source = open(module.__file__).read() + open(plugin.__file__).read()
    for word in ("gmail", "smtp", "send_message", "Duty(\"digest", "bus.publish"):
        assert word not in source, word
