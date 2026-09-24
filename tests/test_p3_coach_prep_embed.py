"""Phase 3 longitudinal coach, pre-call prep and local embeddings, offline.

The coach must carry pattern numbers verbatim and strip any it invents; the
prep brief must put overdue loops first and carry the MEDDPICC questions;
similar() must rank by meaning and do nothing when Ollama is down.
"""
import json
from datetime import datetime, timedelta

from salescoach import repo
from salescoach.intel import coach, embed, prep
from salescoach.memory import patterns
from salescoach.orchestrator import review
from salescoach.schemas.analysis import SellerObservation
from salescoach.sources import paste
from salescoach.store.stores import now
from test_p3_strategy import FakeEmbedder, intel_env, run_call  # noqa: F401  (autouse fixture)

IST_TODAY = datetime.now(prep.history.IST).date()


def _analysed_call(db, day, tags):
    call = repo.create_call(db, source="paste", title=f"Call {day}", started_at=f"2026-08-{day:02d}T10:00:00+00:00",
                            wf_state="done")
    db.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES (?,?,?,?,?)",
               (call, "final", 0, "me", "Okay then, we will see next month."))
    db.execute("INSERT INTO artifacts(call_id,kind,json,created_at) VALUES (?,?,?,?)", (call, "analysis", json.dumps({
        "coaching_insight": {"insight": "Pin dates.", "evidence_turns": [0], "practice_next_call": "Say back owner and date."},
        "verdict": {"label": "held", "one_line": "Held", "rationale": "-"}, "biggest_missed_opportunity": None}), now()))
    tax = patterns.taxonomy()
    patterns.record_observations(db, call, [SellerObservation(
        tag=t, polarity=tax[t]["polarity"], severity="high", contexts=["end_of_call"], evidence_turns=[0],
        evidence_quote="Okay then", confidence="high") for t in tags])
    return call


def _has_table(db, name):
    return db.table_exists(name)


def _nudge(db, call, outcome, shown=1, mode="live"):
    """A row in Phase 2's nudges table, in Phase 2's own columns."""
    db.execute("INSERT INTO nudges(call_id,session,mode,t_call,trigger,text,kind,source,shown,outcome,created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (call, "s1", mode, 12.0, "vague_commitment", "Pin the date", "moment", "fast", shown, outcome, now()))


def _coach_answer(c1, c2, c3):
    return {
        "headline": "You open well and close soft; you did it on 99% of calls.",
        "strengths": [
            {"tag": "secures_specific_next_step", "summary": "Pins a date 50% of the time.",
             "evidence": [{"call_id": c1, "turns": [0], "quote": "Okay then"}]},
            {"tag": "made_up_tag", "summary": "x", "evidence": []},
        ],
        "weaknesses": [{"tag": "accepts_vague_commitments", "summary": "Accepts 'I will try'.",
                        "evidence": [{"call_id": c2, "turns": [0], "quote": "a quote never said"},
                                     {"call_id": "call-bogus", "turns": [0], "quote": ""}]}],
        "trajectory": "Seen on 3 of 4 calls, too early for a trend.",
        "priority_tag": "secures_specific_next_step",
        "priority_why": "It costs you dates.", "practice": "Say back owner and date before you hang up.",
        "well_handled": [{"call_id": c3, "why": "Booked the plant visit.", "evidence": []}],
        "say_differently": [{"situation": "When the buyer says he will try",
                             "instead_of": {"call_id": c2, "turns": [0], "quote": "Okay then"},
                             "say": "Can we put 6 Oct in the calendar now — yes?"}],
    }


def test_coach_report_uses_pattern_numbers_verbatim(db, fake_llm):
    calls = [_analysed_call(db, 1, ["accepts_vague_commitments", "secures_specific_next_step"]),
             _analysed_call(db, 2, ["accepts_vague_commitments"]),
             _analysed_call(db, 3, ["accepts_vague_commitments", "secures_specific_next_step"]),
             _analysed_call(db, 4, [])]
    patterns.recompute(db)
    have_nudges = _has_table(db, "nudges")          # Phase 2's table; never created here
    if have_nudges:
        _nudge(db, calls[0], "followed")
        _nudge(db, calls[0], "ignored", shown=0)    # suppressed candidate: never on screen
        _nudge(db, calls[1], "ignored", mode="replay")
        _nudge(db, calls[1], None)                  # shown, outcome not judged yet
        db.commit()
    fake_llm.responses["CoachReport"] = lambda s, p: _coach_answer(*calls[:3])

    assert coach.refresh(db, trigger="analysis") is not None
    report = coach.latest(db)
    for row in db.execute("SELECT * FROM seller_patterns"):
        snap = report["patterns_snapshot"][row["tag"]]
        assert (snap["frequency"], snap["calls_seen"], snap["calls_window"], snap["trend"]) == \
               (row["frequency"], row["calls_seen"], row["calls_window"], row["trend"])
    assert report["patterns_snapshot"]["accepts_vague_commitments"]["frequency"] == 0.75
    assert "99%" not in report["headline"] and "[number removed]" in report["headline"]
    assert "50%" in report["strengths"][0]["summary"]                  # 50 is secures_specific_next_step's number
    assert "3 of 4" in report["trajectory"]["explanation"]
    assert [s["tag"] for s in report["strengths"]] == ["secures_specific_next_step"]
    weak = report["weaknesses"][0]
    assert len(weak["evidence"]) == 1 and weak["evidence"][0]["quote"] == "" and weak["evidence"][0]["verified"] is False
    assert report["priority"]["tag"] == "accepts_vague_commitments"     # the model's pick was a strength
    assert report["trajectory"]["label"] == "insufficient_data"          # decided by code: fewer than 6 calls
    assert report["say_differently"][0]["instead_of"]["verified"] is True
    assert "—" not in report["say_differently"][0]["say"]
    prompt = [c for c in fake_llm.calls if c["schema"] == "CoachReport"][0]["prompt"]
    assert "75% of the last 4" in prompt
    if have_nudges:
        assert report["nudges"]["counts"] == {"followed": 1} and report["nudges"]["shown"] == 2
        assert report["nudges"]["adherence_pct"] == 100
        assert "Outcome counts (outcome)" in prompt and "Followed 100%" in prompt
    else:
        assert report["nudges"] is None and "has not shown any nudges" in prompt

    before = len(fake_llm.calls)
    assert coach.refresh(db, trigger="review") is None                  # inputs unchanged: cached
    assert len(fake_llm.calls) == before


def test_coach_waits_for_enough_calls(db, fake_llm):
    calls = [_analysed_call(db, 1, ["accepts_vague_commitments"]), _analysed_call(db, 2, ["accepts_vague_commitments"])]
    patterns.recompute(db)
    fake_llm.responses["CoachReport"] = lambda s, p: {**_coach_answer(calls[0], calls[1], calls[1]),
                                                      "strengths": [], "well_handled": []}
    assert coach.refresh(db, trigger="analysis") is None
    assert coach.refresh(db, trigger="manual") is not None               # on demand works with fewer


def test_prep_brief_has_overdue_loops_and_meddpicc_questions(db, fake_llm, tmp_path):
    deal, call, (arjun, me) = run_call(db, fake_llm)
    yesterday = (IST_TODAY - timedelta(days=1)).isoformat()
    review.add_loop(db, call, "Send the corrected savings analysis", "me", "my_action", due_date=yesterday,
                    priority="high")
    db.commit()
    (tmp_path / "contacts" / "arjun-kumar.md").write_text(
        "# Arjun Kumar\n| Email | arjun@northwind.test |\n## Relationship Context\nPPL strategic sourcing head.\n")
    fake_llm.responses["PrepWriting"] = {
        "objective": "Lock the CFO meeting — with a date", "objective_why": "Access is the bottleneck.",
        "opening": "Thanks for making time, Arjun.", "close": "So you'll book Monday 6 Oct with the CFO. Done?",
        "watch_for": ["Another 'I will try'"]}
    bid = prep.generate(db, deal, meeting_title="CFO intro",
                        attendees=["arjun@northwind.test", "cfo@northwind.test"], when="2026-10-06T11:00")
    b = prep.get(db, brief_id=bid)
    # CALL1's own "plant-wise breakdown" loop (high, due 2026-09-04) is overdue too. Overdue loops come
    # first, by priority and then the oldest due date; everything else follows.
    overdue = [l for l in b["loops"] if l["overdue"]]
    assert b["loops"][:len(overdue)] == overdue and len(overdue) < len(b["loops"])
    mine = next(l for l in overdue if l["description"] == "Send the corrected savings analysis")
    assert mine["who"] == "You" and mine["due_date"] == yesterday
    rank = [(prep.tables.SEVERITY_ORDER[l["priority"]], l["due_date"]) for l in overdue]
    assert rank == sorted(rank)
    questions = {g["label"]: g["question"] for g in b["gaps"]}
    assert questions["Decision Process"] == "Who signs the pilot after the CFO meeting?"
    assert "Economic Buyer" not in questions                             # known: not a gap
    first = b["stakeholders"][0]
    assert first["name"] == "Arjun Kumar" and first["attending"] and "strategic sourcing head" in first["contact"]
    assert b["meeting"]["unknown_attendees"] == ["cfo@northwind.test"]
    assert b["writing_source"] == "model" and "—" not in b["writing"]["objective"]
    # a weekday the model pairs with a date is checked against the calendar (which weekday is right depends on the year)
    assert b["writing_notes"] == prep.strategist.weekday_mismatches("Monday 6 Oct", b["today"])
    assert b["next_best_action"]["action"].startswith("Send Arjun") and b["health"]["score"] == 50
    assert b["worked_before"]["status"] == "ok"
    assert b["risks"][0]["type"] in ("weak_urgency", "single_threading")
    text = prep.render_text(b)
    assert "OVERDUE You: Send the corrected savings analysis" in text and "ask: Who signs the pilot" in text

    del fake_llm.responses["PrepWriting"]                                # the writer fails: a template stands in
    b2 = prep.get(db, brief_id=prep.generate(db, deal))
    assert b2["writing_source"] == "fallback" and b2["writing"]["objective"] == b["next_best_action"]["action"]
    assert "prep_writer failed" in b2["writing_error"]


WINDOWS = ("Me: Let us talk about detention charges at the plant.\n"
           "Them: Detention at Pant Nagar costs us lakhs every month.\n"
           "Me: What about invoice follow ups?\n"
           "Them: Invoices are acknowledged late by customers.\n"
           "Me: And carrier check-ins?\n"
           "Them: Carriers never answer calls at night.")


def test_similar_ranks_by_meaning(db):
    fake = FakeEmbedder()
    call = paste.import_text(db, WINDOWS, "Embed call")
    first = embed.index_pending(db, emb=fake)
    assert first["status"] == "ok" and first["indexed"] == 2
    hits = embed.similar(db, "detention charges at the plant", k=2, entity_types=("window",), emb=fake)
    assert [h["entity_id"] for h in hits] == [f"{call}:0", f"{call}:3"] and hits[0]["score"] > hits[1]["score"]
    hits = embed.similar(db, "carriers answer calls at night", k=1, emb=fake)
    assert hits[0]["entity_id"] == f"{call}:3"
    assert embed.index_pending(db, emb=fake)["indexed"] == 0             # nothing new
    db.execute("UPDATE turns SET text='Carriers answer on WhatsApp.' WHERE call_id=? AND idx=5", (call,))
    db.commit()
    assert embed.index_pending(db, emb=fake)["indexed"] == 1             # changed text is re-embedded
    assert embed.similar(db, "detention", entity_types=("claim",), emb=fake) == []


def test_embeddings_are_a_noop_without_ollama(db):
    down = embed.OllamaEmbedder(endpoint="http://127.0.0.1:9", timeout_s=1)
    paste.import_text(db, WINDOWS, "Embed call")
    result = embed.index_pending(db, emb=down)
    assert result["status"] == "unavailable" and result["indexed"] == 0
    assert db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    db.execute("INSERT INTO embeddings(entity_type,entity_id,text,text_sha,model,dim,vector,created_at) "
               "VALUES ('window','x:0','t','s',?,2,?,?)", (down.model, b"\x00" * 8, now()))
    assert embed.similar(db, "detention", emb=down) == []                # the query cannot be embedded
