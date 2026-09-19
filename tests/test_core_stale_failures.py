"""A call processed successfully must not keep reporting its earlier failed attempts."""
from salescoach.orchestrator import bus, workflow
from salescoach.schemas.events import Event
from salescoach.sources import paste
from test_core_pipeline import CALL1, _script, _setup


def test_success_resolves_earlier_failures_for_the_same_call(db, fake_llm):
    deal, people = _setup(db)
    _script(fake_llm)
    call = paste.import_text(db, CALL1, "NWP weekly", deal_id=deal, participants=people)
    # simulate the quota-era failure: the CALL_ENDED event is parked as failed
    db.execute("UPDATE wf_events SET status='failed', attempts=3, error='AgentFailed: timed out' WHERE entity_id=?",
               (call,))
    bus.publish(db, Event(type="PROCESS_CALL", entity_id="other-call", dedupe_key="other"))
    db.execute("UPDATE wf_events SET status='failed' WHERE entity_id='other-call'")
    db.commit()
    assert workflow.run_pipeline(db, call) == "awaiting_review"
    # the run also publishes a fresh POST_CALL_ANALYSIS_COMPLETE (a record, pending); only the old failures matter here
    rows = {r["entity_id"]: (r["status"], r["error"]) for r in db.execute(
        "SELECT entity_id, status, error FROM wf_events WHERE type IN ('CALL_ENDED','PROCESS_CALL')")}
    assert rows[call][0] == "done" and "resolved" in rows[call][1]
    assert rows["other-call"][0] == "failed"                     # other calls' failures are untouched
