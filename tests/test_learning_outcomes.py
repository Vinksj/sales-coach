"""Learning layer, outcomes: the deal stage/status editor (memory gate + history) and the
derived outcomes (business days, windows, facts only, idempotent)."""
import json
from datetime import date

import pytest

from salescoach import learning, repo
from salescoach.execution.cadence import add_business_days
from salescoach.intel import tables
from salescoach.learning import outcomes
from salescoach.memory import gate
from salescoach.store.stores import now
from salescoach.store.db import insert_id
from test_learning_support import day, make_call, make_deal
from test_p4_support import make_email, make_loop

FRI = "2026-09-11T06:00:00+00:00"          # Fri 11 Sep 2026, 11:30 IST


def _reply(db, email_id, thread, received_at, deal=None, mid=None):
    db.execute("INSERT INTO email_replies(message_id,thread_id,email_id,deal_id,from_addr,received_at,body,created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (mid or f"m-{received_at}", thread, email_id, deal, "buyer@x.test",
                                            received_at, "ok", now()))


def _outcome(db, kind, subject_id):
    row = db.execute("SELECT * FROM derived_outcomes WHERE kind=? AND subject_id=?", (kind, str(subject_id))).fetchone()
    return (row["value"], json.loads(row["details"])) if row else None


# ---- the stage / status editor ------------------------------------------------------------------

def test_ensure_columns_is_idempotent(db):
    learning.ensure_columns(db)
    learning.ensure_columns(db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(deals)")}
    assert {"value", "currency", "lost_reason", "close_target"} <= cols


def test_outcome_edit_goes_through_the_gate_and_appends_history(db):
    deal = make_deal(db, "Northwind")
    result = outcomes.set_deal_outcome(db, deal, {"stage": "pilot", "status": "active", "value": "120,000",
                                                  "currency": "usd", "close_target": "2026-12-15"})
    assert set(result["changed"]) == {"stage", "value", "currency", "close_target"}
    row = db.execute("SELECT * FROM deals WHERE node_id=?", (deal,)).fetchone()
    assert (row["stage"], row["value"], row["currency"], row["close_target"]) == ("pilot", 120000.0, "USD", "2026-12-15")
    for field in ("stage", "value", "currency", "close_target"):
        prov = db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field=?", (deal, field)).fetchone()
        assert prov["confidence"] == "user_input", field
    hist = outcomes.stage_history(db, deal)
    assert len(hist) == 1 and (hist[0]["from_stage"], hist[0]["to_stage"], hist[0]["by"]) == ("discovery", "pilot", "user:ui")
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind='deals_stage_changed' AND node_id=?", (deal,)).fetchone()[0] == 1

    # A model's later, weaker opinion is parked as a conflict; it never overwrites the user's stage.
    verdict = gate.propose(db, gate.Proposed(deal, "deals", "stage", "negotiation", "high", {"kind": "call", "ref": "c1"}))
    assert verdict == "conflict"
    assert db.execute("SELECT stage FROM deals WHERE node_id=?", (deal,)).fetchone()[0] == "pilot"
    # No change, no history row.
    assert outcomes.set_deal_outcome(db, deal, {"stage": "pilot", "status": "active"}) == {"changed": [], "history_id": None}
    assert len(outcomes.stage_history(db, deal)) == 1


def test_won_and_lost_need_a_confirm_and_lost_needs_a_reason(db):
    deal = make_deal(db, "OM")
    with pytest.raises(outcomes.OutcomeRefused, match="confirm"):
        outcomes.set_deal_outcome(db, deal, {"status": "won"})
    with pytest.raises(outcomes.OutcomeRefused, match="reason"):
        outcomes.set_deal_outcome(db, deal, {"status": "lost"}, confirmed=True)
    with pytest.raises(outcomes.OutcomeRefused):
        outcomes.format_lost_reason("not_a_code", "")
    with pytest.raises(outcomes.OutcomeRefused):
        outcomes.format_lost_reason("other", "  ")
    assert db.execute("SELECT status FROM deals WHERE node_id=?", (deal,)).fetchone()[0] == "active"
    assert outcomes.stage_history(db, deal) == []

    reason = outcomes.format_lost_reason("competitor", "went with the incumbent's new module")
    outcomes.set_deal_outcome(db, deal, {"status": "lost", "lost_reason": reason}, confirmed=True)
    row = db.execute("SELECT status, lost_reason FROM deals WHERE node_id=?", (deal,)).fetchone()
    assert row["status"] == "lost" and outcomes.lost_reason_parts(row["lost_reason"])[0] == "competitor"
    hist = outcomes.stage_history(db, deal)[0]
    assert (hist["from_status"], hist["to_status"], hist["lost_reason"]) == ("active", "lost", reason)

    # Reopening keeps the reason in the history and clears it on the live deal.
    outcomes.set_deal_outcome(db, deal, {"status": "active"})
    assert db.execute("SELECT lost_reason FROM deals WHERE node_id=?", (deal,)).fetchone()[0] is None
    assert [h["to_status"] for h in outcomes.stage_history(db, deal)] == ["active", "lost"]
    with pytest.raises(outcomes.OutcomeRefused):
        outcomes.set_deal_outcome(db, deal, {"status": "sleeping"})
    with pytest.raises(outcomes.OutcomeRefused):
        outcomes.set_deal_outcome(db, deal, {"value": "a lot"})
    with pytest.raises(KeyError):
        outcomes.set_deal_outcome(db, "deal-nope", {"status": "active"})


# ---- derived outcomes ----------------------------------------------------------------------------

def test_reply_window_is_business_days_from_the_existing_helper(db):
    assert outcomes.business_deadline(date(2026, 9, 11), 5) == add_business_days(date(2026, 9, 11), 5) == date(2026, 9, 18)
    deal = make_deal(db, "NWP")
    in_time = make_email(db, deal, status="sent", sent_at=FRI, thread_id="t1")
    late = make_email(db, deal, status="sent", sent_at=FRI, thread_id="t2")
    silent = make_email(db, deal, status="sent", sent_at=FRI, thread_id="t3")
    draft = make_email(db, deal, status="drafted", thread_id="t4")
    _reply(db, in_time, "t1", "2026-09-18T12:00:00+00:00", deal)      # Fri, the 5th business day
    _reply(db, late, "t2", "2026-09-21T05:00:00+00:00", deal)         # Mon, the 6th
    outcomes.recompute(db, today=date(2026, 9, 30))
    assert _outcome(db, "email_replied", in_time)[0] == 1
    value, details = _outcome(db, "email_replied", late)
    assert value == 0 and details["deadline"] == "2026-09-18" and len(details["late_reply_ids"]) == 1
    assert _outcome(db, "email_replied", silent)[0] == 0
    assert _outcome(db, "email_replied", draft) is None                # never sent: no outcome row

    # Inside the window a silent email is "too early to say", not a no.
    outcomes.recompute(db, today=date(2026, 9, 16))
    assert _outcome(db, "email_replied", silent)[0] is None
    assert _outcome(db, "email_replied", in_time)[0] == 1


def test_a_reply_is_credited_to_the_latest_email_already_sent_in_the_thread(db):
    deal = make_deal(db, "NWP")
    first = make_email(db, deal, status="sent", sent_at="2026-09-01T06:00:00+00:00", thread_id="t1")
    nudge = make_email(db, deal, status="sent", sent_at=FRI, thread_id="t1")
    _reply(db, nudge, "t1", "2026-09-14T06:00:00+00:00", deal)         # poll stores the thread's latest email id
    outcomes.recompute(db, today=date(2026, 9, 30))
    assert _outcome(db, "email_replied", nudge)[0] == 1
    assert _outcome(db, "email_replied", first)[0] == 0                # 5 business days passed before any reply


def test_meeting_after_email_window(db):
    deal, other = make_deal(db, "NWP"), make_deal(db, "Other")
    hit = make_email(db, deal, status="sent", sent_at="2026-09-01T06:00:00+00:00")
    miss = make_email(db, other, status="sent", sent_at="2026-09-01T06:00:00+00:00")

    def meeting(event_id, deal_id, first_seen, start):
        db.execute("INSERT INTO calendar_meetings(event_id,deal_id,title,start_at,first_seen_at) VALUES (?,?,?,?,?)",
                   (event_id, deal_id, "Review", start, first_seen))

    meeting("e0", deal, "2026-08-30T06:00:00+00:00", "2026-09-05T06:00:00+00:00")   # on the calendar before the email
    meeting("e1", deal, "2026-09-10T06:00:00+00:00", "2026-09-20T06:00:00+00:00")   # day 9
    meeting("e2", other, "2026-09-12T06:00:00+00:00", "2026-09-20T06:00:00+00:00")  # day 11: too late
    outcomes.recompute(db, today=date(2026, 9, 30))
    value, details = _outcome(db, "meeting_after_email", hit)
    assert value == 1 and details["event_id"] == "e1"
    assert _outcome(db, "meeting_after_email", miss)[0] == 0
    outcomes.recompute(db, today=date(2026, 9, 5))
    assert _outcome(db, "meeting_after_email", miss)[0] is None


def test_loop_closed_on_time(db):
    deal = make_deal(db, "NWP")
    on_time = make_loop(db, deal, due_date="2026-09-10", status="done", closed_at="2026-09-10T12:00:00+00:00")
    late = make_loop(db, deal, due_date="2026-09-10", status="done", closed_at="2026-09-12T12:00:00+00:00")
    overdue = make_loop(db, deal, due_date="2026-09-10")
    not_due = make_loop(db, deal, due_date="2026-10-10")
    dropped = make_loop(db, deal, due_date="2026-09-10", status="cancelled")
    undated = make_loop(db, deal)
    outcomes.recompute(db, today=date(2026, 9, 15))
    assert [_outcome(db, "loop_closed_on_time", x)[0] for x in (on_time, late, overdue, not_due)] == [1, 0, 0, None]
    assert _outcome(db, "loop_closed_on_time", dropped) is None and _outcome(db, "loop_closed_on_time", undated) is None


def test_call_advanced_is_facts_only(db):
    deal = make_deal(db, "NWP", domain="nwp.test")
    me = repo.ensure_me(db)
    arjun = repo.create_person(db, "Arjun", email="arjun@nwp.test")
    cfo = repo.create_person(db, "Anita", email="anita@nwp.test", title="CFO")
    colleague = repo.create_person(db, "Piyush", email="piyush@tessel.test")
    c1, c2, c3, c4 = (make_call(db, deal, n) for n in (0, 7, 14, 21))
    for call_id, people in ((c1, (me, arjun)), (c2, (me, arjun, cfo)), (c3, (me, arjun, cfo, colleague)),
                            (c4, (me, arjun))):
        for p in people:
            repo.add_participant(db, call_id, p)
    # The analyst calling a call "advanced" is an opinion; it must not move this outcome.
    db.execute("UPDATE artifacts SET json=? WHERE call_id=?", (json.dumps({"verdict": {"label": "advanced"}}), c3))

    # c4: an element goes unknown -> known in the strategist run OF c4, and a buyer loop reported done on c4, on time.
    run = insert_id(db.execute("INSERT INTO agent_runs(agent,call_id,status,started_at) VALUES ('deal_strategist',?,'ok',?)",
                     (c4, now())))
    mid = tables.meddpicc_id(deal, "economic_buyer")
    tables.upsert(db, "meddpicc", mid, {"deal_id": deal, "element": "economic_buyer"}, {"status": "unknown"}, "medium",
                  {"kind": "strategist", "ref": "run:0"})
    tables.upsert(db, "meddpicc", mid, {"deal_id": deal, "element": "economic_buyer"}, {"status": "known"}, "high",
                  {"kind": "strategist", "ref": f"run:{run}", "call": c4})
    loop = make_loop(db, deal, call_id=c1, due_date="2026-12-31", status="done", closed_at=now())
    gate.set_initial(db, loop, "loops", "status", "done", "explicit", {"kind": "call", "ref": c4})
    own_loop = make_loop(db, deal, call_id=c1, owner="me", due_date="2026-12-31", status="done", closed_at=now())
    gate.set_initial(db, own_loop, "loops", "status", "done", "explicit", {"kind": "call", "ref": c4})

    outcomes.recompute(db, today=date(2026, 9, 15))
    assert _outcome(db, "call_advanced", c1) is None                   # nothing to compare the first call with
    value, details = _outcome(db, "call_advanced", c2)
    assert value == 1 and details["new_participants"] == [cfo] and details["previous_call"] == c1
    value, details = _outcome(db, "call_advanced", c3)
    assert value == 0 and not details["new_participants"]              # own-domain colleague, and a model's verdict
    value, details = _outcome(db, "call_advanced", c4)
    assert value == 1 and details["elements_now_known"] == [{"element": "economic_buyer", "to": "known"}]
    assert details["buyer_loops_closed_on_time"] == [loop]             # the seller's own loop does not count

    unanalysed = make_call(db, deal, 28, analysed=False)
    outcomes.recompute(db, today=date(2026, 9, 15))
    assert _outcome(db, "call_advanced", unanalysed) is None


def test_recompute_is_idempotent_and_prunes(db):
    deal = make_deal(db, "NWP")
    email = make_email(db, deal, status="sent", sent_at=FRI, thread_id="t1")
    make_loop(db, deal, due_date="2026-09-10", status="done", closed_at="2026-09-09T12:00:00+00:00")
    first = outcomes.recompute(db, today=date(2026, 9, 30))
    before = [tuple(r) for r in db.execute("SELECT * FROM derived_outcomes ORDER BY id")]
    second = outcomes.recompute(db, today=date(2026, 9, 30))
    assert first["written"] == 3 and second["written"] == 0 and second["removed"] == 0
    assert before == [tuple(r) for r in db.execute("SELECT * FROM derived_outcomes ORDER BY id")]
    assert {c["kind"]: c["n"] for c in outcomes.counts(db)} == {
        "email_replied": 1, "meeting_after_email": 1, "loop_closed_on_time": 1, "call_advanced": 0}
    db.execute("UPDATE emails SET status='failed' WHERE id=?", (email,))
    assert outcomes.recompute(db, today=date(2026, 9, 30))["removed"] == 2
    assert day(0) < day(1)
