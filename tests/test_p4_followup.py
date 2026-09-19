"""Phase 4 follow-up agent: the deterministic checks decide before any model is
asked; the model's five decisions; a nudge is only ever DRAFTED and waits for
Send; follow_up_count moves on a real send only; FOLLOW_UP_DUE is published
once per loop per day; every decision is logged with an agent_runs row."""
import argparse
import json
from datetime import date, timedelta

import pytest

from salescoach import repo
from salescoach.automation import followup
from salescoach.execution import cadence, policy
from salescoach.memory import gate
from salescoach.orchestrator import bus, worker
from salescoach.schemas.events import Event
from salescoach.store.stores import now
from test_p4_support import (ANITA, ARJUN, NUDGE_BODY, OUTSIDER, TODAY, FakeGmail, clock, decision,  # noqa: F401
                             events_of, link_nudge, loop_row, make_email, make_loop, nudge, world)


def _emails(db):
    return db.execute("SELECT COUNT(*) FROM emails").fetchone()[0]


def _open_conflict(db, loop_id):
    return db.execute("SELECT * FROM memory_conflicts WHERE entity_id=? AND field='loops.status' AND status='open'",
                      (loop_id,)).fetchone()


# ---- deterministic checks -------------------------------------------------------------

def _completed(db, w):
    lid = make_loop(db, w.deal)
    gate.park(db, gate.Proposed(lid, "loops", "status", "done", "high", {"kind": "email", "reason": "he wrote it is done"}))
    db.commit()
    return lid, TODAY


def _superseded(db, w):
    return make_loop(db, w.deal, superseded_by="loop-newer"), TODAY


def _deal_status(status):
    def setup(db, w):
        db.execute("UPDATE deals SET status=? WHERE node_id=?", (status, w.deal))
        return make_loop(db, w.deal), TODAY
    return setup


def _replied(db, w):
    lid = make_loop(db, w.deal)
    db.execute("INSERT INTO email_replies(message_id,thread_id,deal_id,from_addr,from_name,received_at,body,created_at) "
               "VALUES ('m-r','t-r',?,?,?,?,?,?)",
               (w.deal, ARJUN, "Arjun Kumar", "2026-09-14T06:00:00+00:00", "Will revert on the CFO meeting.", now()))
    db.commit()
    return lid, TODAY


def _newer_call(state):
    def setup(db, w):
        lid = make_loop(db, w.deal)
        repo.create_call(db, source="paste", title="NWP weekly 2", deal_id=w.deal,
                         started_at="2026-09-10T11:00:00+05:30", wf_state=state)
        db.commit()
        return lid, TODAY
    return setup


def _own(db, w):
    return make_loop(db, w.deal, "Send the plant-wise breakdown", owner="me", owner_name=None, type_="my_action"), TODAY


def _pending(db, w):
    lid = make_loop(db, w.deal)
    link_nudge(db, lid, make_email(db, w.deal), w.deal)
    return lid, TODAY


def _escalated(db, w):
    return make_loop(db, w.deal, escalation="flag_deal_risk:loop-esc-x"), TODAY


def _max(db, w):
    return make_loop(db, w.deal, follow_up_count=2), TODAY


def _too_soon(db, w):
    lid = make_loop(db, w.deal, follow_up_count=1)
    sent = make_email(db, w.deal, status="sent", sent_at="2026-09-14T06:00:00+00:00")
    link_nudge(db, lid, sent, w.deal, eval_date="2026-09-14")
    return lid, TODAY


def _no_recipient(db, w):
    other = repo.create_deal(db, "No contacts yet")
    db.commit()
    return make_loop(db, other, owner_name="Ravi"), TODAY


def _weekend(db, w):
    return make_loop(db, w.deal), date(2026, 9, 19)                 # a Saturday


def _extra_paused(db, lid, w):
    assert loop_row(db, lid)["next_check_at"] == "2026-09-29"       # 10 business days on


def _extra_weekend(db, lid, w):
    assert loop_row(db, lid)["next_check_at"] == "2026-09-21"       # Monday


def _extra_too_soon(db, lid, w):
    assert loop_row(db, lid)["next_check_at"] == "2026-09-16"       # 2 business days after the last nudge


def _extra_replied(db, lid, w):
    assert loop_row(db, lid)["last_activity_at"] == "2026-09-14T06:00:00+00:00"   # the same reply is not re-raised


def _extra_escalated(db, lid, w):
    loop = loop_row(db, lid)
    assert loop["next_check_at"] is None and loop["escalation"].startswith("flag_deal_risk:loop-esc-")
    risk = loop_row(db, loop["escalation"].split(":", 1)[1])
    assert (risk["type"], risk["owner"], risk["review_state"]) == ("deal_risk", "me", "proposed")
    assert json.loads(risk["dependencies"]) == [lid] and "No response after 2 follow-ups" in risk["description"]


def _parked(value):
    def extra(db, lid, w):
        conflict = _open_conflict(db, lid)
        assert conflict["proposed_value"] == value
        assert json.loads(conflict["provenance"])["kind"] == "followup"
    return extra


RULES = [
    ("completed", _completed, "ask_user", None),
    ("superseded", _superseded, "skip", _parked("superseded")),
    ("deal_closed", _deal_status("won"), "close_as_stale", _parked("cancelled")),
    ("deal_paused", _deal_status("paused"), "wait_until", _extra_paused),
    ("replied", _replied, "ask_user", _extra_replied),
    ("newer_call", _newer_call("done"), "ask_user", None),
    ("newer_call_processing", _newer_call("summarized"), "wait_until", None),
    ("own_commitment", _own, "ask_user", None),
    ("nudge_pending", _pending, "ask_user", None),
    ("already_escalated", _escalated, "skip", None),
    ("max_follow_ups", _max, "escalate", _extra_escalated),
    ("too_soon", _too_soon, "wait_until", _extra_too_soon),
    ("no_recipient", _no_recipient, "ask_user", None),
    ("weekend", _weekend, "wait_until", _extra_weekend),
]


@pytest.mark.parametrize("check,setup,expected,extra", RULES, ids=[r[0] for r in RULES])
def test_deterministic_checks_decide_before_any_model(db, world, fake_llm, check, setup, expected, extra):
    lid, today = setup(db, world)
    before = _emails(db)
    results = {r["loop_id"]: r for r in followup.evaluate_due(db, today)}
    r = results[lid]
    assert (r["check"], r["decision"]) == (check, expected), r["rationale"]
    assert fake_llm.calls == []                                     # no model was asked
    assert _emails(db) == before                                    # nothing drafted, nothing sent
    assert loop_row(db, lid)["status"] == "open"                    # nothing closes a loop on its own say-so
    d = db.execute("SELECT * FROM followup_decisions WHERE loop_id=? AND eval_date=?",
                   (lid, today.isoformat())).fetchone()
    assert d["stage"] == "rules" and d["check_name"] == check and d["rationale"]
    run = db.execute("SELECT * FROM agent_runs WHERE id=?", (d["run_id"],)).fetchone()
    assert (run["agent"], run["provider"], run["status"]) == ("followup", "rules", "ok")
    assert json.loads(run["output"])["decision"] == expected
    assert [e["payload"]["loop_id"] for e in events_of(db, "FOLLOW_UP_DUE")] == [lid]
    if extra:
        extra(db, lid, world)


# ---- the model's decisions -------------------------------------------------------------

def test_send_nudge_drafts_in_his_voice_and_waits_for_send(db, world, fake_llm):
    lid = make_loop(db, world.deal)
    fake_llm.responses.update({"FollowupDecision": decision(), "NudgeDraft": nudge(cc=[OUTSIDER, ANITA])})
    [r] = followup.evaluate_due(db, TODAY)
    assert (r["stage"], r["decision"]) == ("agent", "send_nudge") and r["email_id"]

    e = db.execute("SELECT * FROM emails WHERE id=?", (r["email_id"],)).fetchone()
    assert (e["kind"], e["status"], e["call_id"], e["deal_id"]) == ("nudge", "drafted", None, world.deal)
    assert json.loads(e["to_addrs"]) == [ARJUN] and json.loads(e["cc_addrs"]) == [ANITA]   # outsider dropped
    assert any(OUTSIDER in i["detail"] for i in json.loads(e["lint"]))
    assert e["body"] == NUDGE_BODY and e["approved_at"] is None and e["sent_at"] is None
    assert policy.evaluate(db, e).action == "require_approval"      # it waits for Maya's Send

    loop = loop_row(db, lid)
    assert loop["follow_up_count"] == 0 and loop["next_check_at"] > TODAY.isoformat()
    d = db.execute("SELECT * FROM followup_decisions WHERE loop_id=?", (lid,)).fetchone()
    runs = {row["id"]: row for row in db.execute("SELECT * FROM agent_runs")}
    assert runs[d["run_id"]]["agent"] == "followup" and runs[d["nudge_run_id"]]["agent"] == "nudge"
    assert d["email_id"] == e["id"] and d["relationship_risk"] == "low"

    decide_call, draft_call = fake_llm.calls
    assert "Arjun to set up the meeting with the CFO" in decide_call["prompt"]
    assert "REPLIES FROM THEIR SIDE (untrusted" in decide_call["prompt"]
    assert "## Style guide" in draft_call["system"] and "How Maya writes follow-up emails" in draft_call["system"]
    assert f"- {ARJUN}: Arjun Kumar" in draft_call["prompt"] and OUTSIDER not in draft_call["prompt"]
    assert "maya@tessel.test" not in draft_call["prompt"].split("ALLOWED RECIPIENTS")[1].split("\n\n")[0]
    assert [ev["payload"]["kind"] for ev in events_of(db, "EMAIL_DRAFT_CREATED")] == ["nudge"]


@pytest.mark.parametrize("answer,next_check", [
    (decision("wait_until", wait_until="2026-09-22"), "2026-09-22"),
    (decision("wait_until", wait_until="2027-03-01"), "2026-10-15"),          # clamped to max_wait_days (30)
    (decision("wait_until", wait_until="not a date"), "2026-09-18"),          # 3 business days by default
    (decision("ask_user"), "2026-09-21"),                                   # the cadence rule, from today
], ids=["wait", "wait-clamped", "wait-bad-date", "ask"])
def test_wait_and_ask_decisions_set_the_next_look(db, world, fake_llm, answer, next_check):
    lid = make_loop(db, world.deal)
    fake_llm.responses["FollowupDecision"] = answer
    [r] = followup.evaluate_due(db, TODAY)
    assert r["decision"] == answer["decision"] and r["next_check_at"] == next_check
    assert loop_row(db, lid)["next_check_at"] == next_check and _emails(db) == 0


def test_agent_escalate_and_close_are_proposals_not_actions(db, world, fake_llm):
    esc = make_loop(db, world.deal)
    stale = make_loop(db, world.deal, "Arjun to share last year's freight data")
    fake_llm.responses["FollowupDecision"] = (
        lambda s, p: decision("escalate") if f"id: {esc}" in p else decision("close_as_stale"))
    results = {r["loop_id"]: r for r in followup.evaluate_due(db, TODAY)}

    assert results[esc]["decision"] == "escalate"
    risk = loop_row(db, results[esc]["risk_loop_id"])
    assert (risk["type"], risk["review_state"], risk["status"]) == ("deal_risk", "proposed", "open")
    assert loop_row(db, esc)["escalation"] == f"flag_deal_risk:{risk['node_id']}"
    assert loop_row(db, esc)["next_check_at"] is None

    assert results[stale]["decision"] == "close_as_stale"
    assert loop_row(db, stale)["status"] == "open"                  # only proposed
    assert _open_conflict(db, stale)["proposed_value"] == "cancelled"
    assert _emails(db) == 0


@pytest.mark.parametrize("answer", [decision(relationship_risk="high"), decision(still_relevant=False)],
                         ids=["high-risk", "not-relevant"])
def test_a_risky_or_irrelevant_nudge_goes_to_the_user(db, world, fake_llm, answer):
    make_loop(db, world.deal)
    fake_llm.responses["FollowupDecision"] = answer                  # no NudgeDraft answer: it must not be asked
    [r] = followup.evaluate_due(db, TODAY)
    assert r["decision"] == "ask_user" and "waits for your call" in r["rationale"]
    assert [c["schema"] for c in fake_llm.calls] == ["FollowupDecision"] and _emails(db) == 0


def test_a_failed_agent_decides_nothing(db, world, fake_llm):
    make_loop(db, world.deal)
    [r] = followup.evaluate_due(db, TODAY)                          # FakeProvider has no answer at all
    assert (r["check"], r["decision"]) == ("agent_failed", "ask_user")
    statuses = [row[0] for row in db.execute("SELECT status FROM agent_runs WHERE agent='followup' AND provider='fake'")]
    assert statuses == ["invalid", "invalid"] and _emails(db) == 0


def test_template_brackets_are_retried_once_then_the_clean_draft_kept(db, world, fake_llm):
    make_loop(db, world.deal)
    bad = nudge(body="Hi [First name],\n\nAbout the CFO meeting.\n\nThanks,\nMaya")
    fake_llm.responses.update({"FollowupDecision": decision(), "NudgeDraft": [bad, nudge()]})
    [r] = followup.evaluate_due(db, TODAY)
    assert db.execute("SELECT body FROM emails WHERE id=?", (r["email_id"],)).fetchone()[0] == NUDGE_BODY
    assert [row[0] for row in db.execute("SELECT status FROM agent_runs WHERE agent='nudge' ORDER BY id")] == \
        ["invalid", "ok"]
    assert "[First name]" in fake_llm.calls[-1]["prompt"]           # the retry was told what was wrong


def test_a_nudge_that_keeps_its_brackets_is_never_drafted(db, world, fake_llm):
    make_loop(db, world.deal)
    fake_llm.responses.update({"FollowupDecision": decision(),
                               "NudgeDraft": [nudge(subject="Re: [topic]"), nudge(body="Hi [Name], see [link].")]})
    [r] = followup.evaluate_due(db, TODAY)
    assert r["decision"] == "ask_user" and "Drafting the nudge failed" in r["rationale"]
    assert "email_id" not in r and _emails(db) == 0


def test_slots_marker_is_the_one_bracket_a_nudge_may_keep(db, world, fake_llm):
    make_loop(db, world.deal)
    body = "Hi Arjun,\n\nHappy to walk Anita through the numbers.\n[SLOTS]\n\nThanks,\nMaya"
    fake_llm.responses.update({"FollowupDecision": decision(), "NudgeDraft": nudge(body=body)})
    [r] = followup.evaluate_due(db, TODAY)
    row = db.execute("SELECT * FROM emails WHERE id=?", (r["email_id"],)).fetchone()
    assert row["body"] == body
    assert [i["kind"] for i in policy.evaluate(db, row).issues if i["severity"] == "block"] == ["slots"]


# ---- counting sends ---------------------------------------------------------------------

def test_follow_up_count_moves_only_on_a_real_send(db, world, fake_llm, clock):
    lid = make_loop(db, world.deal)
    fake_llm.responses.update({"FollowupDecision": decision(), "NudgeDraft": nudge()})
    [r] = followup.evaluate_due(db, TODAY)
    worker.drain(db)                                                # draft and FOLLOW_UP_DUE events: records only
    assert loop_row(db, lid)["follow_up_count"] == 0

    gmail = FakeGmail()
    policy.approve_and_send(db, r["email_id"], gmail, mode="send")
    assert [m.to for m in gmail.sent] == [[ARJUN]]
    assert loop_row(db, lid)["follow_up_count"] == 0                # counted by the EMAIL_SENT handler
    worker.drain(db)
    loop = loop_row(db, lid)
    assert loop["follow_up_count"] == 1 and loop["next_check_at"] == "2026-09-21"
    assert db.execute("SELECT sent_counted_at FROM followup_decisions WHERE email_id=?",
                      (r["email_id"],)).fetchone()[0]

    # the same send seen again (a duplicate approve, a re-delivered event) is not counted twice
    assert policy.approve_and_send(db, r["email_id"], gmail, mode="send")["duplicate"] is True
    assert followup.on_email_sent(db, Event(type="EMAIL_SENT", payload={"email_id": r["email_id"]})) == 0
    worker.drain(db)
    assert loop_row(db, lid)["follow_up_count"] == 1 and len(gmail.sent) == 1

    text = followup.explain(followup.why(db, r["email_id"]))
    assert "<- drafted this email" in text and "because you approved it" in text


def test_saved_to_gmail_and_call_emails_do_not_count(db, world, fake_llm):
    lid = make_loop(db, world.deal)
    fake_llm.responses.update({"FollowupDecision": decision(), "NudgeDraft": nudge()})
    [r] = followup.evaluate_due(db, TODAY)
    gmail = FakeGmail()
    assert policy.approve_and_send(db, r["email_id"], gmail, mode="draft")["status"] == "saved_to_gmail"
    worker.drain(db)
    assert loop_row(db, lid)["follow_up_count"] == 0 and gmail.sent == [] and len(gmail.drafts) == 1

    call_email = make_email(db, world.deal, kind="followup", status="sent", sent_at=now())
    assert followup.on_email_sent(db, Event(type="EMAIL_SENT", payload={"email_id": call_email})) == 0


# ---- once per loop per day ----------------------------------------------------------------

def test_follow_up_due_is_published_once_per_loop_per_day(db, world, fake_llm):
    lid = make_loop(db, world.deal, "Send the plant-wise breakdown", owner="me", owner_name=None, type_="my_action")
    assert len(followup.evaluate_due(db, TODAY)) == 1
    assert followup.evaluate_due(db, TODAY) == []                   # already decided today
    followup.evaluate_one(db, lid, TODAY)                           # a manual re-look the same day
    assert db.execute("SELECT COUNT(*) FROM followup_decisions WHERE loop_id=?", (lid,)).fetchone()[0] == 2
    due = events_of(db, "FOLLOW_UP_DUE")
    assert [e["dedupe_key"] for e in due] == [f"FOLLOW_UP_DUE:{lid}:2026-09-15"]

    tomorrow = TODAY + timedelta(days=1)
    db.execute("UPDATE loops SET next_check_at=? WHERE node_id=?", (tomorrow.isoformat(), lid))
    db.commit()
    assert len(followup.evaluate_due(db, tomorrow)) == 1
    assert len(events_of(db, "FOLLOW_UP_DUE")) == 2


def test_unscheduled_tracked_loops_get_a_cadence_date(db, world, fake_llm):
    tracked = make_loop(db, world.deal, next_check_at=None, created_at="2026-09-10T10:00:00+05:30")
    unconfirmed = make_loop(db, world.deal, "maybe", next_check_at=None, review_state="proposed", confidence="medium")
    assert followup.schedule_unscheduled(db, TODAY) == 1
    assert loop_row(db, tracked)["next_check_at"] == \
        cadence.next_check(dict(loop_row(db, tracked)), date(2026, 9, 10)).isoformat() == "2026-09-16"
    assert loop_row(db, unconfirmed)["next_check_at"] is None


def test_the_user_can_ask_for_a_nudge_now(db, world, fake_llm, clock):
    lid = make_loop(db, world.deal, next_check_at="2026-09-30")    # not due yet
    fake_llm.responses["NudgeDraft"] = nudge()
    bus.publish(db, Event(type="FOLLOW_UP_NUDGE", entity_id=lid, dedupe_key="nudge-now", payload={"loop_id": lid}))
    db.commit()
    worker.drain(db)
    d = db.execute("SELECT * FROM followup_decisions WHERE loop_id=?", (lid,)).fetchone()
    assert (d["stage"], d["check_name"], d["decision"]) == ("user", "requested", "send_nudge")
    assert db.execute("SELECT status FROM emails WHERE id=?", (d["email_id"],)).fetchone()[0] == "drafted"
    assert [c["schema"] for c in fake_llm.calls] == ["NudgeDraft"]
    assert events_of(db, "FOLLOW_UP_DUE") == []


# ---- CLI -----------------------------------------------------------------------------------

def run_cli(*argv):
    from salescoach.plugins import execution
    parser = argparse.ArgumentParser()
    execution.register_cli(parser.add_subparsers())
    args = parser.parse_args(list(argv))
    args.fn(args)


def test_cli_lists_then_decides_due_loops(db, world, fake_llm, capsys):
    lid = make_loop(db, world.deal, "Send the plant-wise breakdown", owner="me", owner_name=None, type_="my_action")
    run_cli("followups", "--date", "2026-09-15")
    out = capsys.readouterr().out
    assert lid in out and "(add --run to decide them)" in out
    run_cli("followups", "--run", "--date", "2026-09-15")
    assert f"{lid}  [rules/own_commitment] ask you" in capsys.readouterr().out
    assert db.execute("SELECT COUNT(*) FROM followup_decisions").fetchone()[0] == 1
