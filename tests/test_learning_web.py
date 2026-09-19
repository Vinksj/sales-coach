"""Learning layer, the seams: the /learning page, the deal-page outcome editor, the same-origin
guard, the workflow handlers (idempotent, never raising), the daily duty and the CLI."""
import argparse
import json

import pytest
from fastapi.testclient import TestClient

from salescoach import plugins
from salescoach.learning import outcomes, patterns
from salescoach.orchestrator import bus, worker, workflow
from salescoach.plugins import learning as plugin
from salescoach.schemas.events import Event
from salescoach.store import stores
from salescoach.web.app import create_app
from test_learning_support import calls_with_tag, final_snapshot, live_nudge, make_call, make_deal, pattern, sent_edit

ORIGIN = {"origin": "http://127.0.0.1:8140"}
TAG = "avoids_budget"
PID = patterns.pattern_id("seller", TAG)


@pytest.fixture
def client(db):
    return TestClient(create_app(start_worker=False, live_factory=None))


def _populate(db):
    d1, d2 = make_deal(db, "Northwind"), make_deal(db, "Eastline Logistics")
    ids = calls_with_tag(db, TAG, [(d1, 0, True), (d2, 1, True), (d1, 2, True)])
    calls_with_tag(db, "new:avoiding_budget_talk", [(d2, 3, True)])
    live = make_call(db, d1, 4, source="capture", title="Live pilot review")
    final_snapshot(db, live, me_share=0.44, questions=9, objections=("price",))
    for _ in range(15):
        live_nudge(db, live, "dig_deeper", outcome="ignored")
    sent_edit(db, d1, "Hi M,\n\nGreat call!\n\nThanks,\nS", "Hi M,\n\nGood call.\n\nThanks,\nS")
    outcomes.set_deal_outcome(db, d2, {"status": "lost", "lost_reason": outcomes.format_lost_reason("timing", "next FY")},
                              confirmed=True)
    db.commit()
    return d1, d2, ids


def test_plugin_attaches_through_the_seam(db):
    assert ("/learning", "Learning") in plugins.nav()
    assert any(r.path == "/learning" for router in plugins.routers() for r in router.routes)
    workflow.ensure_plugins()
    plugin.register(workflow)
    for event_type in ("LEARNING_RECOMPUTE", "POST_CALL_ANALYSIS_COMPLETE", "EMAIL_SENT", "STAKEHOLDER_REPLY_RECEIVED"):
        assert any(h.__name__.startswith("learning_after_") for h in workflow.HANDLERS[event_type]), event_type
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"deal_stage_history", "derived_outcomes", "pattern_observations", "learned_patterns",
            "learning_proposals"} <= tables
    sub = argparse.ArgumentParser().add_subparsers()
    plugin.register_cli(sub)
    assert "learn" in sub.choices


def test_page_renders_on_an_empty_database(client):
    page = client.get("/learning")
    assert page.status_code == 200
    for needle in ("What the coach believes", "No open proposals", "Nothing observed yet", "No deal has been marked won or lost",
                   "How you sell", "Your email voice", "Follow-up effectiveness", "Live nudges", "Buyer personas", "Objections"):
        assert needle in page.text, needle
    assert '<a href="/learning"' in client.get("/").text                    # the NAV entry


def test_page_renders_populated_with_counts_evidence_and_actions(client, db):
    d1, d2, ids = _populate(db)
    assert client.post("/learning/recompute", headers=ORIGIN, follow_redirects=False).status_code == 303
    text = client.get("/learning").text
    for needle in ("Avoiding the budget conversation", "<b>3</b> calls", "<b>2</b> deals", "emerging",
                   f'href="/calls/{ids[2]}#t3"', "Confirm", "Wrong", "Retire", "Merge into", "Tag merge",
                   "Live nudge weight", "shown 15 times live", "No exclamation marks.", "talk share", "n=1", "0.44",
                   "Price objection", "Observation only", "Wrong timing", "next FY", "Lost by reason",
                   "No causal claim is made before 10 closed deals (1 so far)", "<b>15</b> shown live"):
        assert needle in text, needle
    assert "%" not in text.split('id="family-seller"')[1].split('id="family-email_voice"')[0]   # labels and n, never a percentage


def test_posts_without_an_origin_are_refused(client, db):
    d1, _, _ = _populate(db)
    plugin.run_recompute(db)
    [prop] = [p for p in patterns.open_proposals(db) if p["kind"] == "merge"]
    for url, data in (("/learning/patterns", {"id": PID, "action": "wrong"}),
                      ("/learning/recompute", {}),
                      (f"/learning/proposals/{prop['id']}/accept", {}),
                      (f"/deals/{d1}/outcome", {"status": "won", "confirm": "yes"})):
        assert client.post(url, data=data, follow_redirects=False).status_code == 403, url
        bad = client.post(url, data=data, headers={"origin": "http://evil.example"}, follow_redirects=False)
        assert bad.status_code == 403, url
    assert pattern(db, PID)["user_state"] is None
    assert db.execute("SELECT status FROM deals WHERE node_id=?", (d1,)).fetchone()[0] == "active"
    assert db.execute("SELECT status FROM learning_proposals WHERE id=?", (prop["id"],)).fetchone()[0] == "open"


def test_pattern_actions_and_proposals_from_the_page(client, db):
    _populate(db)
    plugin.run_recompute(db)
    post = lambda data: client.post("/learning/patterns", data=data, headers=ORIGIN, follow_redirects=False)   # noqa: E731
    assert post({"id": PID, "action": "confirm"}).status_code == 303
    assert pattern(db, PID)["user_state"] == "confirmed"
    assert post({"id": PID, "action": "wrong"}).status_code == 303
    assert (pattern(db, PID)["status"], pattern(db, PID)["n_calls"]) == ("retired", 0)
    assert post({"id": PID, "action": "undo"}).status_code == 303
    assert (pattern(db, PID)["status"], pattern(db, PID)["n_calls"]) == ("active", 3)
    assert post({"id": "lp:seller:global:new:nope", "action": "retire"}).status_code == 404
    assert "Choose+the+pattern" in post({"id": PID, "action": "merge"}).headers["location"]
    assert "Unknown+action" in post({"id": PID, "action": "explode"}).headers["location"]

    source = patterns.pattern_id("seller", "new:avoiding_budget_talk")
    [merge] = [p for p in patterns.open_proposals(db) if p["kind"] == "merge"]
    r = client.post(f"/learning/proposals/{merge['id']}/accept", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and pattern(db, source)["merged_into"] == PID and pattern(db, PID)["n_calls"] == 4
    assert post({"id": source, "action": "unmerge"}).status_code == 303 and pattern(db, PID)["n_calls"] == 3
    assert post({"id": source, "action": "merge", "target": PID}).status_code == 303 and pattern(db, PID)["n_calls"] == 4

    [weight] = [p for p in patterns.open_proposals(db) if p["kind"] == "trigger_weight"]
    r = client.post(f"/learning/proposals/{weight['id']}/dismiss", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and patterns.open_proposals(db) == []
    again = client.post(f"/learning/proposals/{weight['id']}/accept", headers=ORIGIN, follow_redirects=False)
    assert "already+decided" in again.headers["location"]
    assert client.post("/learning/proposals/9999/accept", headers=ORIGIN, follow_redirects=False).status_code == 404


def test_deal_outcome_editor_on_the_deal_page(client, db):
    deal = make_deal(db, "Northwind")
    db.commit()
    assert f'data-intel-src="/deals/{deal}/outcome"' in client.get(f"/deals/{deal}").text
    frag = client.get(f"/deals/{deal}/outcome")
    assert frag.status_code == 200 and "Stage and outcome" in frag.text and "Chose a competitor" in frag.text
    assert client.get("/deals/deal-nope/outcome").status_code == 404
    url = f"/deals/{deal}/outcome"

    r = client.post(url, headers=ORIGIN, follow_redirects=False, data={"status": "lost", "stage": "proposal"})
    assert "confirm+box" in r.headers["location"] and r.headers["location"].endswith("#outcome")
    r = client.post(url, headers=ORIGIN, follow_redirects=False, data={"status": "lost", "confirm": "yes"})
    assert "needs+a+reason" in r.headers["location"]
    assert db.execute("SELECT status, stage FROM deals WHERE node_id=?", (deal,)).fetchone()[:] == ("active", "discovery")

    r = client.post(url, headers=ORIGIN, follow_redirects=False, data={
        "status": "lost", "stage": "proposal", "confirm": "yes", "lost_reason_code": "no_budget",
        "lost_reason_text": "capex frozen", "value": "50000", "currency": "usd", "close_target": "2026-11-30"})
    assert "Saved" in r.headers["location"]
    row = db.execute("SELECT * FROM deals WHERE node_id=?", (deal,)).fetchone()
    assert (row["status"], row["stage"], row["lost_reason"], row["value"]) == ("lost", "proposal", "no_budget: capex frozen", 50000.0)
    assert db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='status'", (deal,)).fetchone()[0] == "user_input"
    hist = outcomes.stage_history(db, deal)
    assert len(hist) == 1 and (hist[0]["to_status"], hist[0]["to_stage"]) == ("lost", "proposal")
    frag = client.get(url).text
    assert "active → lost" in frag and "capex frozen" in frag
    # Editing the value of a lost deal does not ask for the reason again.
    r = client.post(url, headers=ORIGIN, follow_redirects=False, data={"status": "lost", "stage": "proposal", "value": "60000",
                                                                       "currency": "usd", "close_target": "2026-11-30"})
    assert "Saved" in r.headers["location"] and len(outcomes.stage_history(db, deal)) == 1
    page = client.get("/learning").text
    assert "No budget" in page and "capex frozen" in page


def test_handlers_recompute_after_the_three_events_and_never_raise(db, monkeypatch):
    monkeypatch.setattr(bus, "RETRY_BACKOFF_S", 0)
    workflow.ensure_plugins()
    # Only the learner's handlers: the other plugins' handlers for these events call agents (no model in these tests).
    monkeypatch.setattr(workflow, "HANDLERS", {})
    plugin.register(workflow)
    assert set(workflow.HANDLERS) == {"LEARNING_RECOMPUTE", "POST_CALL_ANALYSIS_COMPLETE", "EMAIL_SENT",
                                      "STAKEHOLDER_REPLY_RECEIVED"}
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, TAG, [(d1, 0, True), (d2, 1, True), (d1, 2, True)])
    for n, event_type in enumerate(("LEARNING_RECOMPUTE", "POST_CALL_ANALYSIS_COMPLETE", "EMAIL_SENT",
                                    "STAKEHOLDER_REPLY_RECEIVED")):
        db.execute("DELETE FROM learned_patterns")
        db.commit()
        bus.publish(db, Event(type=event_type, entity_id=d1, payload={"email_id": -1, "reply_id": -1},
                              dedupe_key=f"test:{event_type}:{n}"))
        db.commit()
        worker.drain(db)
        assert pattern(db, PID)["label"] == "emerging", event_type
        assert json.loads(stores.get_state(db, "learning:last_run"))["trigger"]
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE status='failed'").fetchone()[0] == 0

    # A failure inside the learner is rolled back, recorded, and never reaches the pipeline.
    def boom(conn):
        conn.execute("INSERT INTO learning_proposals(kind,subject,summary,created_at) VALUES ('merge','half','x','now')")
        raise RuntimeError("counting went wrong")
    monkeypatch.setattr(patterns, "recompute", boom)
    bus.publish(db, Event(type="EMAIL_SENT", entity_id=d1, payload={"email_id": -1}, dedupe_key="test:boom"))
    db.commit()
    worker.drain(db)
    assert db.execute("SELECT status FROM wf_events WHERE dedupe_key='test:boom'").fetchone()[0] == "done"
    assert "counting went wrong" in json.loads(stores.get_state(db, "learning:last_error"))["error"]
    assert db.execute("SELECT COUNT(*) FROM learning_proposals WHERE subject='half'").fetchone()[0] == 0
    assert plugin.run_recompute(db)["error"].startswith("RuntimeError")


def test_cli_and_daily_duty(db, monkeypatch, capsys):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, TAG, [(d1, 0, True), (d2, 1, True), (d1, 2, True)])
    assert plugin.cmd_learn(argparse.Namespace(recompute=True, show=True)) is None
    out = capsys.readouterr().out
    assert "Avoiding the budget conversation" in out and "calls 3, deals 2" in out and "emerging" in out
    assert "email_replied" in out and PID in out
    plugin.cmd_learn(argparse.Namespace(recompute=False, show=False))
    assert "How you sell" in capsys.readouterr().out

    started = {}
    from salescoach.automation import scheduler
    monkeypatch.setattr(scheduler, "start", lambda db_path, stop, duties=None: started.update(duties=duties))
    plugin.start_background("ignored.db", None)
    [duty] = started["duties"]
    assert duty.name == "learning" and duty.interval_s() == 24 * 3600
    assert duty.run(db)["trigger"] == "daily"
