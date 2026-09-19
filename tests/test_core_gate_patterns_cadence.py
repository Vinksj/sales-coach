import json
from datetime import date

from salescoach import repo
from salescoach.execution import cadence
from salescoach.memory import gate, patterns
from salescoach.schemas.analysis import SellerObservation
from salescoach.store.stores import engine, now


def _deal(conn):
    acct = repo.create_account(conn, "Northwind", ["northwind.test"])
    return repo.create_deal(conn, "NWP pilot", account_id=acct, stage="evaluation")


def test_gate_rules(db):
    deal = _deal(db)
    call = {"kind": "call", "ref": "call-1"}
    assert gate.propose(db, gate.Proposed(deal, "deals", "next_step", "CFO meeting", "explicit", call)) == "applied"
    # weaker claim cannot overwrite an explicit one
    assert gate.propose(db, gate.Proposed(deal, "deals", "next_step", "send deck", "medium", call)) == "conflict"
    assert db.execute("SELECT next_step FROM deals WHERE node_id=?", (deal,)).fetchone()[0] == "CFO meeting"
    conflict = db.execute("SELECT * FROM memory_conflicts WHERE status='open'").fetchone()
    assert conflict["proposed_value"] == "send deck"
    # Maya's input wins and closes the conflict
    assert gate.propose(db, gate.Proposed(deal, "deals", "next_step", "pilot agreement", "user_input",
                                          {"kind": "user_input"})) == "applied"
    assert db.execute("SELECT COUNT(*) FROM memory_conflicts WHERE status='open'").fetchone()[0] == 0
    # every applied change is an event
    kinds = [r["kind"] for r in db.execute("SELECT kind FROM events WHERE node_id=?", (deal,))]
    assert kinds.count("deals_next_step_changed") == 2


def test_gate_resolve_conflict_accept(db):
    deal = _deal(db)
    gate.propose(db, gate.Proposed(deal, "deals", "stage", "pilot", "explicit", {"kind": "call"}))
    gate.propose(db, gate.Proposed(deal, "deals", "stage", "lost", "low", {"kind": "call"}))
    cid = db.execute("SELECT id FROM memory_conflicts").fetchone()[0]
    assert gate.resolve_conflict(db, cid, accept=True)
    assert db.execute("SELECT stage FROM deals WHERE node_id=?", (deal,)).fetchone()[0] == "lost"


def test_gate_rejects_ungated_field(db):
    deal = _deal(db)
    try:
        gate.propose(db, gate.Proposed(deal, "deals", "name; DROP TABLE deals", "x", "high", {}))
    except ValueError:
        return
    raise AssertionError("ungated field accepted")


def _analysed_call(db, day, tags):
    call = repo.create_call(db, source="paste", started_at=f"2026-08-{day:02d}T10:00:00+00:00", wf_state="done")
    db.execute("INSERT INTO artifacts(call_id,kind,json,created_at) VALUES (?,?,?,?)", (call, "analysis", "{}", now()))
    obs = [SellerObservation(tag=t, polarity="weakness", severity="high", contexts=["end_of_call"],
                             evidence_turns=[1], evidence_quote="ok then", confidence="high") for t in tags]
    patterns.record_observations(db, call, obs)
    return call


def test_patterns_frequency_trend_status(db):
    # older 5 calls: vague commitments on every call; newest 5: on one call
    for day in range(1, 6):
        _analysed_call(db, day, ["accepts_vague_commitments"])
    for day in range(6, 11):
        _analysed_call(db, day, ["accepts_vague_commitments"] if day == 10 else ["avoids_budget"])
    patterns.recompute(db)
    row = db.execute("SELECT * FROM seller_patterns WHERE tag='accepts_vague_commitments'").fetchone()
    assert row["frequency"] == 0.6 and row["calls_seen"] == 6 and row["calls_window"] == 10
    assert row["trend"] == "improving" and row["status"] == "active"
    assert row["recommended_intervention"].startswith("Before ending")
    budget = db.execute("SELECT * FROM seller_patterns WHERE tag='avoids_budget'").fetchone()
    assert budget["trend"] == "worsening"
    assert patterns.active_priority(db)["tag"] == "accepts_vague_commitments"


def test_new_tag_is_candidate_until_second_call(db):
    _analysed_call(db, 1, ["talks over the buyer"])
    patterns.recompute(db)
    row = db.execute("SELECT * FROM seller_patterns").fetchone()
    assert row["tag"] == "new:talks_over_the_buyer" and row["status"] == "candidate"
    assert row["trend"] == "insufficient_data"


def test_cadence_rules():
    wed = date(2026, 9, 2)
    assert cadence.next_check({"type": "prospect_action", "due_date_confidence": "explicit",
                               "due_date": "2026-09-04"}, wed) == date(2026, 9, 7)   # Fri + 1 business day
    assert cadence.next_check({"type": "prospect_action", "due_date_confidence": "unknown"}, wed) == date(2026, 9, 8)
    assert cadence.next_check({"type": "my_action", "due_date_confidence": "explicit",
                               "due_date": "2026-09-05"}, wed) == date(2026, 9, 5)
    assert cadence.next_check({"type": "deal_risk", "priority": "critical"}, wed) == date(2026, 9, 3)
    assert cadence.next_check({"type": "follow_up"}, wed) == date(2026, 9, 9)
