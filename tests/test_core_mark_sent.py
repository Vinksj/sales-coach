"""Sending a Gmail draft by hand must count as a send everywhere downstream."""
import json

from salescoach import repo
from salescoach.execution import policy
from salescoach.store.stores import now


def _saved(db, kind="followup", with_call=True):
    acct = repo.create_account(db, "Northwind", ["northwind.test"])
    deal = repo.create_deal(db, "NWP", account_id=acct)
    call = repo.create_call(db, source="paste", deal_id=deal, wf_state="awaiting_review") if with_call else None
    cur = db.execute("INSERT INTO emails(call_id,deal_id,kind,to_addrs,subject,body,draft_body,status,created_at) "
                     "VALUES (?,?,?,?,?,?,?,'saved_to_gmail',?)",
                     (call, deal, kind, json.dumps(["m@northwind.test"]), "S", "B", "B", now()))
    db.commit()
    return cur.lastrowid, call, deal


def test_mark_sent_records_the_send_and_publishes_it_once(db):
    eid, call, _ = _saved(db)
    assert policy.mark_sent_manually(db, eid)
    assert db.execute("SELECT status FROM emails WHERE id=?", (eid,)).fetchone()[0] == "sent"
    assert not policy.mark_sent_manually(db, eid)                    # only a saved draft can be marked
    events = db.execute("SELECT entity_id, payload FROM wf_events WHERE type='EMAIL_SENT'").fetchall()
    assert len(events) == 1 and events[0]["entity_id"] == call


def test_nudge_page_mark_sent_route(db):
    from fastapi.testclient import TestClient
    from salescoach.web.app import create_app
    eid, _, deal = _saved(db, kind="nudge", with_call=False)
    client = TestClient(create_app(start_worker=False, live_factory=None, hub=None))
    r = client.post(f"/nudges/{eid}/mark-sent", headers={"origin": "http://127.0.0.1:8140"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.execute("SELECT status FROM emails WHERE id=?", (eid,)).fetchone()[0] == "sent"
    assert db.execute("SELECT entity_id FROM wf_events WHERE type='EMAIL_SENT'").fetchone()[0] == deal


def test_a_nudge_without_a_call_is_traced_to_its_deal(db):
    eid, _, deal = _saved(db, kind="nudge", with_call=False)
    policy.mark_sent_manually(db, eid)
    ev = db.execute("SELECT entity_id, payload FROM wf_events WHERE type='EMAIL_SENT'").fetchone()
    assert ev["entity_id"] == deal and json.loads(ev["payload"])["kind"] == "nudge"
