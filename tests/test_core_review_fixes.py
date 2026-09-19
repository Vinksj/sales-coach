"""Regression tests for the 2026-09-12 core review. Every case here reproduced a
real bug on a throwaway database before its fix; each now asserts the fixed
behaviour."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from salescoach import repo
from salescoach.execution import policy
from salescoach.integrations import jarvis_bridge as bridge
from salescoach.memory import gate
from salescoach.orchestrator import context, review, workflow
from salescoach.store import stores
from salescoach.store.stores import engine, now
from salescoach.validators import evidence, voice_lint
from salescoach.validators.gates import can_feed_external

needs_jarvis = pytest.mark.skipif(not (bridge.JARVIS_DIR / "commitments.py").exists(), reason="jarvis not installed")


def T(text, ch="them"):
    return {"text": text, "channel": ch, "quality": "ok", "bleed_flag": 0}


def _setup(db, source="paste", state="awaiting_review", email="arjun@northwind.test"):
    acct = repo.find_account_by_domain(db, "northwind.test") or repo.create_account(db, "Northwind", ["northwind.test"])
    deal = repo.create_deal(db, "NWP", account_id=acct)
    call = repo.create_call(db, source=source, deal_id=deal, wf_state=state)
    person = repo.find_person_by_email(db, email) or repo.create_person(db, "Arjun", email=email, account_id=acct)
    repo.add_participant(db, call, person)
    repo.add_participant(db, call, repo.ensure_me(db))
    repo.link_deal_person(db, deal, person)
    db.commit()
    return deal, call


def _loop(db, deal, call, desc, review_state="proposed", world_id=None, world_link=None, conf="high"):
    lid = repo.new_id("loop")
    engine.add_node(db, "t", id=lid, type="loop", title=desc, status="full")
    db.execute("INSERT INTO loops(node_id,deal_id,call_id,type,description,owner,source,confidence,evidence_quote,"
               "status,review_state,world_commitment_id,world_link,created_at) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
               (lid, deal, call, "my_action", desc, "me", "explicit_commitment", conf, "q", review_state,
                world_id, world_link, now()))
    gate.set_initial(db, lid, "loops", "status", "open", conf, {"kind": "call", "ref": call})
    db.commit()
    return lid


def _email(db, deal, call, status="drafted"):
    cur = db.execute("INSERT INTO emails(call_id,deal_id,to_addrs,cc_addrs,subject,body,draft_body,version,status,"
                     "created_at,updated_at) VALUES (?,?,?,?,?,?,?,1,?,?,?)",
                     (call, deal, json.dumps(["arjun@northwind.test"]), "[]", "Re: pilot", "Hi Arjun", "Hi Arjun",
                      status, now(), now()))
    db.commit()
    return cur.lastrowid


# ---- evidence -----------------------------------------------------------------

def test_negated_quote_is_not_evidence():
    turns = {0: T("No, we will not sign the pilot agreement this month.")}
    for quote in ("we will sign the pilot agreement this month", "sign the pilot agreement this month"):
        conf, _, chk = evidence.judge("explicit", quote, [0], turns, owner="prospect")
        assert conf == "low" and chk.how == "negated" and not chk.found


def test_owner_check_uses_the_turn_the_quote_matched():
    turns = {4: T("Sure.", "me"), 5: T("I'll send the deck on Friday.", "them")}
    conf, _, chk = evidence.judge("explicit", "I'll send the deck on Friday", [4], turns, owner="me")
    assert conf == "medium" and chk.channels == {"them"}


def test_loose_match_never_above_medium():
    turns = {0: T("I will send you the revised deck on Friday", "me")}
    conf, _, chk = evidence.judge("explicit", "I will send the revised deck on Friday", [0], turns, owner="me")
    assert chk.how == "fuzzy" and conf == "medium"


def test_recommended_items_need_confirmation_to_reach_an_email():
    assert not can_feed_external("explicit", "proposed", "recommended")
    assert can_feed_external("explicit", "confirmed", "recommended")
    assert can_feed_external("high", "proposed", "explicit_commitment")


def test_placeholder_scaffolding_blocks_sending():
    kinds = {(i.kind, i.severity) for i in voice_lint.lint("NWP", "Hi [first name],\n\nThanks\n[sign-off]")}
    assert ("placeholder", "block") in kinds


# ---- memory gate --------------------------------------------------------------

def test_value_user_cleared_stays_protected(db):
    deal, _ = _setup(db)
    P = gate.Proposed
    gate.propose(db, P(deal, "deals", "next_step", "Pilot review", "user_input", {"kind": "user_input"}))
    gate.propose(db, P(deal, "deals", "next_step", None, "user_input", {"kind": "user_input"}))
    assert gate.propose(db, P(deal, "deals", "next_step", "Send pricing", "low", {"kind": "call"})) == "conflict"


def test_users_edit_accepts_only_the_matching_proposal(db):
    deal, call = _setup(db)
    lid = _loop(db, deal, call, "Send deck")
    review.confirm_loop(db, lid)
    P = gate.Proposed
    gate.propose(db, P(lid, "loops", "status", "done", "high", {"kind": "call"}))
    gate.propose(db, P(lid, "loops", "status", "cancelled", "high", {"kind": "call"}))
    review.edit_loop(db, lid, status="done")
    rows = {r["proposed_value"]: r["status"] for r in db.execute(
        "SELECT proposed_value, status FROM memory_conflicts WHERE entity_id=?", (lid,))}
    assert rows == {"done": "accepted", "cancelled": "rejected"}


# ---- jarvis bridge -------------------------------------------------------------

@pytest.fixture
def world(db, tmp_path, monkeypatch):
    path = tmp_path / "world.db"
    monkeypatch.setattr(stores, "WORLD_DB", path)
    monkeypatch.setenv("WORLD_DB", str(path))
    bridge._commitments("list")
    return path


def _w(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _land_native(counterparty="arjun@northwind.test"):
    payload = {"text": "Send Arjun the savings model", "quote": "I'll send the model", "direction": "owed_by_me",
               "channel": "granola", "source_id": "granola:abc"}
    if counterparty is not None:
        payload["counterparty"] = counterparty
    return json.loads(bridge._commitments("land", json.dumps(payload)).splitlines()[-1])["id"]


@needs_jarvis
def test_rejecting_a_model_guessed_link_keeps_the_jarvis_commitment(db, world):
    wid = _land_native()
    deal, call = _setup(db)
    lid = _loop(db, deal, call, "Share savings model", world_id=wid, world_link="adopted")
    review.reject_loop(db, lid)
    db.commit()
    bridge.sync(db)
    assert _w(world).execute("SELECT status FROM nodes WHERE id=?", (wid,)).fetchone()["status"] == "full"
    assert db.execute("SELECT world_commitment_id FROM loops WHERE node_id=?", (lid,)).fetchone()[0] is None


@needs_jarvis
def test_unconfirmed_guessed_link_is_not_kept_alive(db, world):
    wid = _land_native()
    deal, call = _setup(db)
    _loop(db, deal, call, "Share savings model", world_id=wid, world_link="adopted")
    assert bridge.sync(db)["kept_alive"] == 0


@needs_jarvis
def test_a_loop_the_user_reopens_is_reopened_in_jarvis(db, world):
    deal, call = _setup(db)
    lid = _loop(db, deal, call, "Send plant breakdown", review_state="confirmed")
    bridge.sync(db)
    wid = db.execute("SELECT world_commitment_id FROM loops WHERE node_id=?", (lid,)).fetchone()[0]
    review.edit_loop(db, lid, status="done")
    db.commit()
    bridge.sync(db)
    assert _w(world).execute("SELECT status FROM nodes WHERE id=?", (wid,)).fetchone()["status"] == "closed"
    review.edit_loop(db, lid, status="open")
    db.commit()
    result = bridge.sync(db)
    assert db.execute("SELECT status FROM loops WHERE node_id=?", (lid,)).fetchone()[0] == "open"
    assert _w(world).execute("SELECT status FROM nodes WHERE id=?", (wid,)).fetchone()["status"] == "full"
    assert result["read_back"][0]["action"] == "reopened_in_jarvis"


@needs_jarvis
def test_blank_counterparty_matches_no_call(db, world):
    _land_native(counterparty=None)
    assert context.world_commitments([{"email": "someone@unrelated.test", "name": "Someone", "is_me": 0}]) == []


@needs_jarvis
def test_claims_only_processed_calls_with_external_domains(db, world):
    _, failed = _setup(db, source="capture", state="capture_failed")
    _, errored = _setup(db, source="capture", state="quality_done")
    repo.update_call(db, errored, wf_error="summarized: AgentFailed: boom")
    _, internal = _setup(db, source="capture", state="done", email="piyush@tessel.test")
    _, good = _setup(db, source="capture", state="awaiting_review")
    db.commit()
    bridge.claim_calls(db)
    claims = json.loads(_w(world).execute("SELECT value FROM state WHERE key='sales:calls'").fetchone()[0])
    assert [c["call_id"] for c in claims] == [good] and claims[0]["domains"] == ["northwind.test"]


# ---- send path ----------------------------------------------------------------

class OkGmail:
    address = "maya@tessel.test"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return {"message_id": f"m{len(self.sent)}", "thread_id": "t"}


class TimeoutGmail(OkGmail):
    def send(self, msg):
        self.sent.append(msg)                 # Gmail accepted it...
        raise TimeoutError("read timed out")  # ...but the response never arrived


def test_timeout_blocks_resend_until_the_user_confirms(db):
    deal, call = _setup(db)
    eid = _email(db, deal, call)
    first = TimeoutGmail()
    with pytest.raises(TimeoutError):
        policy.approve_and_send(db, eid, first)
    retry = OkGmail()
    with pytest.raises(policy.SendRefused):
        policy.approve_and_send(db, eid, retry)
    assert not retry.sent
    assert policy.acknowledge_not_sent(db, eid)
    policy.approve_and_send(db, eid, retry)
    assert retry.sent[0].message_id == first.sent[0].message_id      # same Message-ID on every attempt


def test_timeout_recovered_from_the_sent_folder(db):
    deal, call = _setup(db)
    eid = _email(db, deal, call)
    with pytest.raises(TimeoutError):
        policy.approve_and_send(db, eid, TimeoutGmail())
    finder = OkGmail()
    finder.find_sent = lambda rfc822: {"message_id": "m-found", "thread_id": "t"}
    result = policy.approve_and_send(db, eid, finder)
    assert result["recovered"] and not finder.sent
    assert db.execute("SELECT status FROM emails WHERE id=?", (eid,)).fetchone()[0] == "sent"


def test_definite_failure_can_be_retried(db):
    deal, call = _setup(db)
    eid = _email(db, deal, call)

    class Rejected(OkGmail):
        def send(self, msg):
            raise type("HttpError", (Exception,), {})("400") if False else _http_error(400)

    with pytest.raises(Exception):
        policy.approve_and_send(db, eid, Rejected())
    assert db.execute("SELECT status FROM emails WHERE id=?", (eid,)).fetchone()[0] == "failed"
    assert policy.approve_and_send(db, eid, OkGmail())["status"] == "sent"


def _http_error(status):
    exc = RuntimeError(f"HTTP {status}")
    exc.resp = SimpleNamespace(status=status)
    return exc


def test_redraft_retires_failed_rows_and_finished_calls_keep_their_state(db, monkeypatch):
    class FakeEmailAgent:
        def run(self, conn, ctx):
            return SimpleNamespace(to=["arjun@northwind.test"], cc=[], subject="Re: pilot v2", body="Hi again",
                                   rationale="r"), None, None, None

    monkeypatch.setattr(workflow, "EmailAgent", FakeEmailAgent)
    deal, call = _setup(db)
    old = _email(db, deal, call, status="failed")
    workflow.run_pipeline(db, call, from_step="email_drafted", force=True)
    assert db.execute("SELECT status FROM emails WHERE id=?", (old,)).fetchone()[0] == "rejected"
    new = db.execute("SELECT id FROM emails WHERE call_id=? AND status='drafted'", (call,)).fetchone()[0]
    policy.approve_and_send(db, new, OkGmail())
    review.email_done(db, call, sent=True)
    assert workflow.run_pipeline(db, call, from_step="email_drafted", force=True) == "done"
    assert db.execute("SELECT COUNT(*) FROM emails WHERE call_id=? AND status='drafted'", (call,)).fetchone()[0] == 0


# ---- web ----------------------------------------------------------------------

def test_other_localhost_port_cannot_press_send(db):
    from fastapi.testclient import TestClient
    from salescoach.web.app import create_app
    deal, call = _setup(db)
    eid = _email(db, deal, call)
    sent = OkGmail()
    client = TestClient(create_app(start_worker=False, gmail_factory=lambda: sent, live_factory=None, hub=None))
    r = client.post(f"/emails/{eid}/send", data={"to": "attacker@evil.test", "subject": "Invoice", "body": "wire"},
                    headers={"origin": "http://localhost:6666"}, follow_redirects=False)
    assert r.status_code == 403 and not sent.sent
    assert client.get("/", headers={"host": "evil.test"}).status_code == 403
