"""Jarvis bridge against a throwaway world.db driven by jarvis's real commitments.py.

WORLD_DB is honoured by jarvis/stores.py, so nothing here touches the live
operator store.
"""
import json
import subprocess

import pytest

from salescoach import repo
from salescoach.integrations import jarvis_bridge as bridge
from salescoach.orchestrator import review
from salescoach.store import stores
from salescoach.store.stores import engine, now

pytestmark = pytest.mark.skipif(not (bridge.JARVIS_DIR / "commitments.py").exists(), reason="jarvis not installed")


@pytest.fixture
def world(db, tmp_path, monkeypatch):
    path = tmp_path / "world.db"
    monkeypatch.setattr(stores, "WORLD_DB", path)
    monkeypatch.setenv("WORLD_DB", str(path))
    bridge._commitments("list")                  # jarvis initialises the schema on first use
    assert path.exists()
    return path


def _world(path):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _loop(db, description, type_="my_action", review_state="confirmed"):
    acct = repo.find_account_by_domain(db, "northwind.test") or repo.create_account(db, "Northwind", ["northwind.test"])
    deal = repo.create_deal(db, "NWP", account_id=acct)
    call = repo.create_call(db, source="paste", deal_id=deal, wf_state="awaiting_review")
    arjun = repo.find_person_by_email(db, "arjun@northwind.test") or repo.create_person(
        db, "Arjun Kumar", email="arjun@northwind.test", account_id=acct)
    repo.add_participant(db, call, arjun)
    lid = repo.new_id("loop")
    engine.add_node(db, "test", id=lid, type="loop", title=description, status="full")
    db.execute("INSERT INTO loops(node_id,deal_id,call_id,type,description,owner,source,confidence,evidence_quote,"
               "status,review_state,created_at) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
               (lid, deal, call, type_, description, "me" if type_ == "my_action" else "prospect",
                "explicit_commitment", "explicit", "I'll send it Friday", review_state, now()))
    db.commit()
    return lid, deal, call


def test_mirror_is_idempotent_and_confirmed_only(db, world):
    lid, _, _ = _loop(db, "Send plant-wise breakdown")
    unconfirmed, _, _ = _loop(db, "Maybe share Rajpura data", review_state="proposed")
    result = bridge.sync(db)
    assert len(result["mirrored"]) == 1
    wid = db.execute("SELECT world_commitment_id FROM loops WHERE node_id=?", (lid,)).fetchone()[0]
    w = _world(world)
    row = w.execute("SELECT n.status, c.direction, c.counterparty, s.uri FROM nodes n JOIN commitments c "
                    "ON c.node_id=n.id JOIN sources s ON s.node_id=n.id WHERE n.id=?", (wid,)).fetchone()
    assert (row["status"], row["direction"], row["counterparty"], row["uri"]) == \
        ("full", "owed_by_me", "arjun@northwind.test", f"sales:{lid}")
    assert db.execute("SELECT world_commitment_id FROM loops WHERE node_id=?", (unconfirmed,)).fetchone()[0] is None
    bridge.sync(db)
    assert w.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1


def test_keep_alive_and_close_propagates(db, world):
    lid, _, _ = _loop(db, "Send plant-wise breakdown")
    bridge.sync(db)
    wid = db.execute("SELECT world_commitment_id FROM loops WHERE node_id=?", (lid,)).fetchone()[0]
    w = _world(world)
    assert w.execute("SELECT COUNT(*) FROM events WHERE node_id=? AND kind='sales_loop_active'",
                     (wid,)).fetchone()[0] == 1
    review.edit_loop(db, lid, status="done")
    db.commit()
    bridge.sync(db)
    assert w.execute("SELECT status FROM nodes WHERE id=?", (wid,)).fetchone()[0] == "closed"


def test_owner_done_in_jarvis_flows_back_but_sweep_is_undone(db, world):
    closed_lid, _, _ = _loop(db, "Send plant-wise breakdown")
    swept_lid, _, _ = _loop(db, "Arjun sets up CFO meeting", type_="prospect_action")
    bridge.sync(db)
    ids = {r["node_id"]: r["world_commitment_id"] for r in db.execute("SELECT * FROM loops")}
    bridge._commitments("close", ids[closed_lid], "--evidence", "owner by email: sent it")
    bridge._commitments("drop", ids[swept_lid], "--reason", "auto-dropped: 15d no movement (restore to undo)")
    result = bridge.sync(db)
    assert {c["action"] for c in result["read_back"]} == {"done", "restored_in_jarvis"}
    assert db.execute("SELECT status FROM loops WHERE node_id=?", (closed_lid,)).fetchone()[0] == "done"
    assert db.execute("SELECT status FROM loops WHERE node_id=?", (swept_lid,)).fetchone()[0] == "open"
    assert _world(world).execute("SELECT status FROM nodes WHERE id=?", (ids[swept_lid],)).fetchone()[0] == "full"
    prov = db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='status'",
                      (closed_lid,)).fetchone()[0]
    assert prov == "user_input"


def test_claim_calls_published(db, world):
    _, _, call = _loop(db, "Send plant-wise breakdown")
    bridge.sync(db)
    value = _world(world).execute("SELECT value FROM state WHERE key='sales:calls'").fetchone()[0]
    claimed = json.loads(value)
    assert claimed[0]["call_id"] == call and claimed[0]["domains"] == ["northwind.test"]


def test_bootstrap_import_links_existing_commitments(db, world):
    _, deal, _ = _loop(db, "placeholder", review_state="rejected")
    arjun = repo.find_person_by_email(db, "arjun@northwind.test")
    repo.link_deal_person(db, deal, arjun)
    db.commit()
    bridge._commitments("land", json.dumps({"text": "Arjun to send carton dimensions", "quote": "I'll send dims",
                                            "direction": "owed_to_me", "counterparty": "arjun@northwind.test",
                                            "channel": "granola", "source_id": "granola:abc"}))
    bridge._commitments("land", json.dumps({"text": "Unrelated", "quote": "x", "direction": "owed_by_me",
                                            "counterparty": "someone@else.test", "channel": "gmail",
                                            "source_id": "email:1"}))
    assert len(bridge.bootstrap_import(db, deal, dry_run=True)["would_import"]) == 1
    created = bridge.bootstrap_import(db, deal)["imported"]
    assert len(created) == 1
    row = db.execute("SELECT * FROM loops WHERE node_id=?", (created[0],)).fetchone()
    assert row["type"] == "prospect_action" and row["review_state"] == "confirmed" and row["source"] == "world"
    assert bridge.bootstrap_import(db, deal)["imported"] == []      # linked, never duplicated
