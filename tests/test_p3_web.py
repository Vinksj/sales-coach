"""Phase 3 routes and fragments: they render, edits land as user_input behind the
gate, queued requests are done by the worker, and the same-origin guard holds.
The worker thread is never started; worker.drain runs queued events."""
import pytest
from fastapi.testclient import TestClient

from salescoach.intel import strategist, tables
from salescoach.orchestrator import worker
from salescoach.web.app import create_app
from test_p3_strategy import NBA, intel_env, run_call  # noqa: F401  (autouse fixture)

ORIGIN = {"origin": "http://127.0.0.1:8140"}


@pytest.fixture
def client(db):
    return TestClient(create_app(start_worker=False, live_factory=None))


def _prov(db, entity, field):
    row = db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field=?", (entity, field)).fetchone()
    return row[0] if row else None


def test_intel_pages_render(client, db, fake_llm):
    deal, call, (arjun, me) = run_call(db, fake_llm)
    page = client.get(f"/deals/{deal}").text
    assert f'data-intel-src="/deals/{deal}/intel"' in page and "/static/intel.js" in page
    assert "interest high, urgency unproven" in page and "Reconciled verdict" in page
    assert "Strong momentum, the CFO meeting is coming" in page          # both stances on the page

    frag = client.get(f"/deals/{deal}/intel")
    assert frag.status_code == 200
    for needle in ("Deal intelligence", "Next best action", NBA, "Capped at 50", "Arjun Kumar", "CFO (name unknown)",
                   'pos pos-champion', "Economic Buyer", "Who signs the pilot after the CFO meeting?", "single threading",
                   "voiceprints", "Re-run strategy", "/prep"):
        assert needle in frag.text, needle
    assert client.get("/deals/deal-nope/intel").status_code == 404

    data = client.get("/intel/deals.json").json()
    assert data[deal]["score"] == 50 and data[deal]["next_best_action"] == NBA
    assert f'data-intel-health="{deal}"' in client.get("/deals").text
    coach_page = client.get("/coach").text
    assert 'data-intel-src="/coach/intel"' in coach_page and "How you sell" in coach_page
    assert "No longitudinal report yet" in client.get("/coach/intel").text
    prep_page = client.get(f"/deals/{deal}/prep")
    assert prep_page.status_code == 200 and "No brief yet" in prep_page.text
    for asset in ("/static/intel.js", "/static/intel.css"):
        assert client.get(asset).status_code == 200


def test_edits_are_user_input_and_survive_the_strategist(client, db, fake_llm):
    deal, call, (arjun, me) = run_call(db, fake_llm)
    sid = tables.stakeholder_id(deal, arjun)
    r = client.post(f"/deals/{deal}/stakeholders/{arjun}", headers=ORIGIN, follow_redirects=False, data={
        "role": "Sourcing head", "position": "skeptic", "incentives": "lower cost\nfewer escalations", "concerns": ""})
    assert r.status_code == 303 and r.headers["location"].endswith(f"#stake-{arjun}")
    row = db.execute("SELECT * FROM stakeholders WHERE id=?", (sid,)).fetchone()
    assert (row["position"], row["role"]) == ("skeptic", "Sourcing head")
    assert tables._loads(row["incentives"], []) == ["lower cost", "fewer escalations"]
    assert _prov(db, sid, "position") == "user_input" and _prov(db, sid, "influence") == "high"
    bad = client.post(f"/deals/{deal}/stakeholders/{arjun}", headers=ORIGIN, follow_redirects=False,
                      data={"position": "friend"})
    assert "position must be one of" in bad.headers["location"].replace("+", " ")
    assert client.post(f"/deals/{deal}/stakeholders/person-nope", headers=ORIGIN, data={"position": "neutral"},
                       follow_redirects=False).status_code == 404

    client.post(f"/deals/{deal}/meddpicc/paper_process", headers=ORIGIN, follow_redirects=False,
                data={"status": "known", "what_we_know": "PO through procurement", "gap": "", "next_question": ""})
    mid = tables.meddpicc_id(deal, "paper_process")
    assert db.execute("SELECT status FROM meddpicc WHERE id=?", (mid,)).fetchone()[0] == "known"
    assert _prov(db, mid, "status") == "user_input"
    client.post(f"/deals/{deal}/risks/weak_urgency/status", headers=ORIGIN, data={"status": "dismissed"},
                follow_redirects=False)

    strategist.run_for_deal(db, deal, force=True)
    assert db.execute("SELECT position FROM stakeholders WHERE id=?", (sid,)).fetchone()[0] == "skeptic"
    assert db.execute("SELECT status FROM meddpicc WHERE id=?", (mid,)).fetchone()[0] == "known"
    assert db.execute("SELECT status FROM deal_risks WHERE id=?",
                      (tables.risk_id(deal, "weak_urgency"),)).fetchone()[0] == "dismissed"
    frag = client.get(f"/deals/{deal}/intel").text
    assert "Needs your decision" in frag and 'class="you-set"' in frag

    conflict = db.execute("SELECT id FROM memory_conflicts WHERE entity_id=? AND field='stakeholders.position' "
                          "AND status='open'", (sid,)).fetchone()["id"]
    r = client.post(f"/conflicts/{conflict}/resolve", headers=ORIGIN, follow_redirects=False,
                    data={"accept": "1", "next": f"/deals/{deal}#intel"})
    assert r.headers["location"].startswith(f"/deals/{deal}")
    assert db.execute("SELECT position FROM stakeholders WHERE id=?", (sid,)).fetchone()[0] == "champion"

    refused = client.post(f"/deals/{deal}/stakeholders/{arjun}", headers={"origin": "http://evil.test"},
                          data={"position": "blocker"}, follow_redirects=False)
    assert refused.status_code == 403
    assert db.execute("SELECT position FROM stakeholders WHERE id=?", (sid,)).fetchone()[0] == "champion"


def test_requests_are_queued_and_done_by_the_worker(client, db, fake_llm):
    deal, call, _ = run_call(db, fake_llm)
    fake_llm.responses["PrepWriting"] = {"objective": "Lock the CFO meeting date", "objective_why": "Access.",
                                         "opening": "Thanks for making time.", "close": "So 6 Oct it is?",
                                         "watch_for": []}
    fake_llm.responses["CoachReport"] = {
        "headline": "Early read: you close soft.", "strengths": [],
        "weaknesses": [{"tag": "accepts_vague_commitments", "summary": "Accepts 'I will try'.",
                        "evidence": [{"call_id": call, "turns": [4], "quote": "What happens if nothing changes"}]}],
        "trajectory": "Too early to say.", "priority_tag": "accepts_vague_commitments", "priority_why": "Dates slip.",
        "practice": "Say back owner and date.", "well_handled": [], "say_differently": []}

    r = client.post(f"/deals/{deal}/prep", headers=ORIGIN, follow_redirects=False,
                    data={"title": "CFO intro", "attendees": "arjun@northwind.test, cfo@northwind.test", "when": ""})
    assert r.status_code == 303
    assert "being prepared" in client.get(f"/deals/{deal}/prep").text
    client.post(f"/deals/{deal}/strategy", headers=ORIGIN, follow_redirects=False)
    assert "A strategy run is queued" in client.get(f"/deals/{deal}/intel").text
    client.post("/coach/report", headers=ORIGIN, follow_redirects=False)
    strategy_calls = sum(c["schema"] == "DealStrategy" for c in fake_llm.calls)

    assert worker.drain(db) == 3
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE status!='done'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_state WHERE key LIKE 'intel:prep_request:%'").fetchone()[0] == 0
    page = client.get(f"/deals/{deal}/prep").text
    assert "Lock the CFO meeting date" in page and "Not on the map: cfo@northwind.test" in page
    assert "Send the plant-wise" in page or "plant-wise" in page
    assert sum(c["schema"] == "DealStrategy" for c in fake_llm.calls) == strategy_calls + 1   # forced re-run
    coach_frag = client.get("/coach/intel").text
    assert "Early read: you close soft." in coach_frag and "Accepting vague commitments" in coach_frag
