"""End-to-end post-call pipeline on a fake model provider.

Covers the vertical slice minus audio: import -> quality -> summary ->
analysis -> actions -> reconcile -> email -> review -> send, plus the
guards: fabricated quotes lose confidence, garbled evidence is capped,
duplicates merge, weak loop updates wait for review, stale calls get no email,
an invalid model answer is retried once.
"""
import json

from salescoach import repo
from salescoach.orchestrator import review, worker
from salescoach.execution import policy
from salescoach.sources import paste

CALL1 = """Me: Thanks for making time, Arjun. Let me walk you through the three levers.
Them: Sure. Before that, our CFO will want to see the savings split by plant.
Me: Understood. I'll send you the plant-wise breakdown by Friday.
Them: Okay. And I'll try to set up a meeting with our CFO and CEO, probably first week of next month.
Me: Great. What happens if nothing changes on load planning this year?
Them: Hi chloral cara march salrat weather.
Me: Right. So the pilot would start at Pant Nagar."""

CALL2 = """Me: Second call. Did the CFO meeting get fixed?
Them: Yes, the CFO meeting is done, we met him on Monday and he is positive.
Me: Good. I'll send the plant-wise breakdown to you today itself.
Them: Maybe we will also look at Rajpura later."""

QUALITY = {"overall_score": 0.85, "lang_mix": "hinglish",
           "flagged_turns": [{"idx": 5, "quality": "garbled", "note": "Hindi rendered as English"}],
           "cannot_judge": [], "summary": "Mostly clear; one garbled turn."}

SUMMARY = {"what_happened": "Walked Arjun through the levers.",
           "key_discussions": [{"text": "Savings by plant", "evidence_turns": [1]}],
           "decisions": [], "commitments": [], "open_questions": [],
           "risks": [{"text": "made up", "evidence_turns": [42]}], "next_step": None}


def analysis(quote_ok=True):
    return {
        "claims": [
            {"subject": "deal.economic_buyer", "statement": "CFO wants plant-wise savings", "kind": "fact",
             "confidence": "explicit", "evidence_turns": [1],
             "evidence_quote": "our CFO will want to see the savings split by plant"},
            {"subject": "deal.metrics", "statement": "Budget approved", "kind": "fact", "confidence": "explicit",
             "evidence_turns": [1], "evidence_quote": "we have budget approved for this"},
        ],
        "gaps": [{"lens": "MEDDPICC", "element": "Decision Process", "missing": "who signs",
                  "why_it_matters": "pilot stalls", "question_to_ask": "Who signs the pilot?"}],
        "seller": [{"dimension": "discovery", "judged": True, "assessment": "ok", "evidence_turns": [4]}],
        "observations": [{"tag": "accepts_vague_commitments", "polarity": "weakness", "severity": "high",
                          "contexts": ["end_of_call"], "evidence_turns": [3, 4],
                          "evidence_quote": "Great. What happens if nothing changes", "confidence": "high"}],
        "assessments": [{"subject": "deal.momentum", "stance": "interest high, urgency unproven",
                         "confidence": "medium", "rationale": "no date", "evidence_turns": [3]}],
        "verdict": {"label": "held", "one_line": "Interest, no commitment", "rationale": "vague CFO meeting"},
        "what_changed": ["CFO named as reviewer"],
        "biggest_missed_opportunity": {"evidence_turns": [3], "what_happened": "accepted 'try to'",
                                       "what_to_do_instead": "pin the date", "suggested_words": "Can we lock 6 Oct?"},
        "coaching_insight": {"insight": "Pin dates.", "evidence_turns": [3], "practice_next_call": "Say back owner and date."},
    }


def action(**kw):
    base = {"description": "", "type": "my_action", "owner": "me", "owner_name": None,
            "source": "explicit_commitment", "evidence_quote": "", "evidence_turns": [], "confidence": "explicit",
            "priority": "high", "due_date": None, "due_date_confidence": "unknown", "follow_up_required": False,
            "follow_up_strategy": "", "dependencies": []}
    base.update(kw)
    return base


ACTIONS1 = {"actions": [
    action(description="Send plant-wise savings breakdown to Arjun", evidence_quote="I'll send you the plant-wise breakdown by Friday",
           evidence_turns=[2], due_date="2026-09-04", due_date_confidence="explicit"),
    action(description="Arjun to set up meeting with CFO and CEO", type="prospect_action", owner="prospect",
           owner_name="Arjun Kumar", source="implied_commitment", confidence="high",
           evidence_quote="I'll try to set up a meeting with our CFO and CEO", evidence_turns=[3],
           due_date="2026-10-09", due_date_confidence="inferred", follow_up_required=True,
           follow_up_strategy="Nudge once after 9 Oct"),
    action(description="Prospect to sign pilot agreement", type="prospect_action", owner="prospect",
           evidence_quote="we will sign the pilot agreement next week", evidence_turns=[6]),
    action(description="Map the approval path for the pilot", type="deal_risk", owner="me", source="recommended",
           confidence="medium", priority="critical"),
], "loop_updates": [], "notes": ""}

EMAIL = {"to": ["arjun@northwind.test", "stranger@evil.test"], "cc": [],
         "subject": "NWP: plant-wise savings and the CFO meeting",
         "body": "Hi Arjun,\n\nGood speaking today — thanks.\n\nNext step: CFO meeting on [SLOTS].\n\nBest,\nMaya",
         "rationale": "short call, short email"}


def _setup(db):
    acct = repo.create_account(db, "Northwind", ["northwind.test"])
    deal = repo.create_deal(db, "NWP pilot", account_id=acct)
    arjun = repo.create_person(db, "Arjun Kumar", email="arjun@northwind.test", account_id=acct)
    repo.link_deal_person(db, deal, arjun, role="champion")
    me = repo.ensure_me(db)
    db.commit()
    return deal, [arjun, me]


def _script(fake, actions2=None):
    first_quality = dict(QUALITY)
    fake.responses.update({
        # first answer violates the schema (missing fields): must be retried once
        "QualityReport": [{"overall_score": 2}, first_quality, first_quality],
        "CallSummary": lambda s, p: SUMMARY,
        "CallAnalysis": lambda s, p: analysis(),
        "ActionExtraction": lambda s, p: actions2 if "Second call" in p else ACTIONS1,
        "EmailDraft": lambda s, p: EMAIL,
    })


def test_full_pipeline(db, fake_llm):
    deal, people = _setup(db)
    _script(fake_llm)
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    assert worker.drain(db) >= 1
    row = repo.get_call(db, call)
    assert row["wf_state"] == "awaiting_review", row["wf_error"]

    # retried once after the invalid quality answer; both attempts recorded
    runs = db.execute("SELECT agent, status FROM agent_runs WHERE agent='quality' ORDER BY id").fetchall()
    assert [r["status"] for r in runs] == ["invalid", "ok"]
    assert db.execute("SELECT quality FROM turns WHERE call_id=? AND idx=5", (call,)).fetchone()[0] == "garbled"

    # fabricated claim lost its confidence; supported one kept it
    claims = {r["statement"]: r["confidence"] for r in db.execute("SELECT * FROM claims WHERE call_id=?", (call,))}
    assert claims == {"CFO wants plant-wise savings": "explicit", "Budget approved": "low"}

    loops = {r["description"]: r for r in db.execute("SELECT * FROM loops WHERE call_id=?", (call,))}
    assert loops["Send plant-wise savings breakdown to Arjun"]["confidence"] == "explicit"
    assert loops["Arjun to set up meeting with CFO and CEO"]["source"] == "implied_commitment"
    assert loops["Prospect to sign pilot agreement"]["confidence"] == "low"       # quote not in transcript
    assert loops["Map the approval path for the pilot"]["source"] == "recommended"
    assert all(r["review_state"] == "proposed" for r in loops.values())
    assert loops["Send plant-wise savings breakdown to Arjun"]["next_check_at"] == "2026-09-04"

    # seller memory recorded, provenance kept
    assert db.execute("SELECT COUNT(*) FROM seller_patterns").fetchone()[0] == 1
    assert db.execute("SELECT uri FROM sources WHERE node_id=?",
                      (loops["Send plant-wise savings breakdown to Arjun"]["node_id"],)).fetchone()[0] \
        == f"{call}#turns=2"

    # email: stranger dropped, em dash fixed, [SLOTS] blocks sending
    email = db.execute("SELECT * FROM emails WHERE call_id=?", (call,)).fetchone()
    assert json.loads(email["to_addrs"]) == ["arjun@northwind.test"]
    assert "—" not in email["body"]
    assert any(i["kind"] == "slots" for i in json.loads(email["lint"]))
    prompt = next(c["prompt"] for c in fake_llm.calls if c["schema"] == "EmailDraft")
    assert "sign pilot agreement" not in prompt      # low-confidence item never reaches the email

    # review: confirm, edit the draft, send exactly once
    for lid in loops:
        pass
    review.confirm_loop(db, loops["Send plant-wise savings breakdown to Arjun"]["node_id"])
    review.reject_loop(db, loops["Prospect to sign pilot agreement"]["node_id"])
    review.update_email(db, email["id"], email["subject"],
                        email["body"].replace("CFO meeting on [SLOTS]", "CFO meeting on Tue 6 Oct, 11am"),
                        ["arjun@northwind.test"], [])
    db.commit()

    class Gmail:
        sent = []

        def send(self, msg):
            self.sent.append(msg)
            return {"message_id": "m1", "thread_id": "t1"}

    gmail = Gmail()
    assert policy.approve_and_send(db, email["id"], gmail)["status"] == "sent"
    assert policy.approve_and_send(db, email["id"], gmail)["duplicate"]
    assert len(gmail.sent) == 1


def test_second_call_merges_and_updates_loops(db, fake_llm):
    deal, people = _setup(db)
    _script(fake_llm)
    call1 = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    loops1 = {r["description"]: r["node_id"] for r in db.execute("SELECT * FROM loops WHERE call_id=?", (call1,))}
    meeting = loops1["Arjun to set up meeting with CFO and CEO"]
    deck = loops1["Send plant-wise savings breakdown to Arjun"]
    actions2 = {"actions": [
        action(description="Send plant-wise savings breakdown to Arjun",
               evidence_quote="I'll send the plant-wise breakdown to you today itself", evidence_turns=[2]),
    ], "loop_updates": [
        {"loop_id": meeting, "proposed_status": "done", "reason": "met CFO Monday",
         "evidence_quote": "the CFO meeting is done, we met him on Monday", "evidence_turns": [1],
         "confidence": "explicit", "superseded_by_action": None},
        {"loop_id": deck, "proposed_status": "waiting", "reason": "maybe",
         "evidence_quote": "Maybe we will also look at Rajpura later", "evidence_turns": [3],
         "confidence": "medium", "superseded_by_action": None},
        {"loop_id": "loop-doesnotexist", "proposed_status": "done", "reason": "x", "evidence_quote": "x",
         "evidence_turns": [0], "confidence": "explicit", "superseded_by_action": None},
    ], "notes": ""}
    _script(fake_llm, actions2)
    call2 = paste.import_text(db, CALL2, "NWP second", deal_id=deal, participants=people)
    worker.drain(db)
    assert repo.get_call(db, call2)["wf_state"] == "awaiting_review", repo.get_call(db, call2)["wf_error"]
    # duplicate merged into the existing loop, not re-created
    assert db.execute("SELECT COUNT(*) FROM loops WHERE call_id=?", (call2,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind='loop_reaffirmed' AND node_id=?", (deck,)).fetchone()[0] == 1
    # strong evidence applied; weak one parked for review; unknown loop ignored
    assert db.execute("SELECT status FROM loops WHERE node_id=?", (meeting,)).fetchone()[0] == "done"
    assert db.execute("SELECT status FROM loops WHERE node_id=?", (deck,)).fetchone()[0] == "open"
    parked = db.execute("SELECT * FROM memory_conflicts WHERE entity_id=?", (deck,)).fetchone()
    assert parked["proposed_value"] == "waiting" and parked["status"] == "open"
    assert review.resolve_proposal(db, parked["id"], accept=True)
    assert db.execute("SELECT status FROM loops WHERE node_id=?", (deck,)).fetchone()[0] == "waiting"


def test_stale_call_gets_no_email(db, fake_llm):
    deal, people = _setup(db)
    _script(fake_llm)
    call = paste.import_text(db, CALL1, "Old call", deal_id=deal, participants=people,
                             started_at="2026-06-01T10:00:00+00:00")
    worker.drain(db)
    assert repo.get_call(db, call)["wf_state"] == "awaiting_review"
    assert db.execute("SELECT COUNT(*) FROM emails WHERE call_id=?", (call,)).fetchone()[0] == 0
    assert not any(c["schema"] == "EmailDraft" for c in fake_llm.calls)


def test_rerun_reuses_cached_steps(db, fake_llm):
    deal, people = _setup(db)
    _script(fake_llm)
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    worker.drain(db)
    before = len(fake_llm.calls)
    from salescoach.orchestrator import workflow
    workflow.run_pipeline(db, call, from_step="quality_done", until="analyzed")
    # quality, summary, analysis inputs unchanged -> no new model calls
    assert len(fake_llm.calls) == before
