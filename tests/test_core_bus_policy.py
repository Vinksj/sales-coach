import json

import pytest

from salescoach import config, repo
from salescoach.execution import policy
from salescoach.orchestrator import bus
from salescoach.schemas.events import Event
from salescoach.store.stores import now
from salescoach.store.db import insert_id


def test_bus_dedupe_claim_fail_recover(db, monkeypatch):
    monkeypatch.setattr(bus, "RETRY_BACKOFF_S", 0)
    assert bus.publish(db, Event(type="CALL_ENDED", entity_id="c1", dedupe_key="CALL_ENDED:c1"))
    assert not bus.publish(db, Event(type="CALL_ENDED", entity_id="c1", dedupe_key="CALL_ENDED:c1"))
    db.commit()
    ev = bus.claim_next(db)
    assert ev.type == "CALL_ENDED" and bus.claim_next(db) is None
    assert bus.fail(db, ev.event_id, "boom") == "pending"
    ev = bus.claim_next(db)
    bus.recover_running(db)                         # simulated crash while running
    ev = bus.claim_next(db)
    assert bus.fail(db, ev.event_id, "boom") == "failed"   # third attempt parks it
    assert bus.claim_next(db) is None


class FakeGmail:
    def __init__(self, fail=False):
        self.sent, self.drafts, self.fail = [], [], fail

    def send(self, msg):
        if self.fail:
            raise TimeoutError("network")
        self.sent.append(msg)
        return {"message_id": f"m{len(self.sent)}", "thread_id": "t1"}

    def save_draft(self, msg):
        self.drafts.append(msg)
        return {"draft_id": "d1", "message_id": "m-d", "thread_id": "t1"}


def _email(db, body="Hi Arjun,\n\nGood speaking today.\n\nThanks", to=("arjun@northwind.test",)):
    acct = repo.find_account_by_domain(db, "northwind.test") or repo.create_account(db, "Northwind", ["northwind.test"])
    deal = repo.create_deal(db, "NWP", account_id=acct)
    call = repo.create_call(db, source="paste", deal_id=deal, wf_state="awaiting_review")
    arjun = repo.find_person_by_email(db, "arjun@northwind.test") or repo.create_person(
        db, "Arjun Kumar", email="arjun@northwind.test", account_id=acct)
    repo.add_participant(db, call, arjun)
    repo.add_participant(db, call, repo.ensure_me(db))
    cur = db.execute(
        "INSERT INTO emails(call_id,deal_id,to_addrs,cc_addrs,subject,body,draft_body,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,'drafted',?)",
        (call, deal, json.dumps(list(to)), "[]", "NWP: plant-wise savings", body, body, now()))
    db.commit()
    return insert_id(cur)


def test_send_exactly_once(db):
    eid = _email(db)
    gmail = FakeGmail()
    first = policy.approve_and_send(db, eid, gmail)
    second = policy.approve_and_send(db, eid, gmail)
    assert first["status"] == "sent" and second["duplicate"] is True
    assert len(gmail.sent) == 1
    row = db.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    assert row["gmail_message_id"] == "m1" and row["idempotency_key"]
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='EMAIL_SENT'").fetchone()[0] == 1


def test_blocked_recipient_and_slots(db):
    eid = _email(db, to=("cfo@elsewhere.test",))
    with pytest.raises(policy.SendRefused, match="not on this call"):
        policy.approve_and_send(db, eid, FakeGmail())
    eid2 = _email(db, body="Next step: meet on [SLOTS].\n\nThanks")
    with pytest.raises(policy.SendRefused, match="calendar"):
        policy.approve_and_send(db, eid2, FakeGmail())


def test_failed_send_is_never_retried_silently(db):
    eid = _email(db)
    with pytest.raises(TimeoutError):
        policy.approve_and_send(db, eid, FakeGmail(fail=True))
    row = db.execute("SELECT status, error FROM emails WHERE id=?", (eid,)).fetchone()
    assert row["status"] == "sending" and row["error"].startswith(policy.UNKNOWN_DELIVERY)
    with pytest.raises(policy.SendRefused, match="Gmail Sent"):
        policy.approve_and_send(db, eid, FakeGmail())


def test_stuck_sending_refuses(db):
    eid = _email(db)
    db.execute("UPDATE emails SET status='sending' WHERE id=?", (eid,))
    db.commit()
    with pytest.raises(policy.SendRefused, match="Gmail Sent"):
        policy.approve_and_send(db, eid, FakeGmail())


def test_save_to_drafts_and_edit_recorded(db):
    eid = _email(db)
    db.execute("UPDATE emails SET body=? WHERE id=?", ("Hi Arjun,\n\nShort and edited.\n\nThanks", eid))
    db.commit()
    gmail = FakeGmail()
    assert policy.approve_and_send(db, eid, gmail, mode="draft")["status"] == "saved_to_gmail"
    assert not gmail.sent and len(gmail.drafts) == 1
    assert db.execute("SELECT COUNT(*) FROM email_edits WHERE email_id=?", (eid,)).fetchone()[0] == 1


def test_auto_send_policy_still_requires_approval(db, monkeypatch):
    eid = _email(db)
    monkeypatch.setattr(config, "load", lambda name: {"email_policy": "LOW_RISK_FOLLOWUPS_AUTO_SEND"}
                        if name == "policy" else {})
    row = db.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    decision = policy.evaluate(db, row)
    assert decision.action == "require_approval" and "still needs your Send" in decision.reason
