"""Quota refusals: no fallback, no spent retries, the event waits."""
from salescoach import repo
from salescoach.orchestrator import bus, worker
from salescoach.providers.base import RateLimited
from salescoach.sources import paste


class Limited:
    name = "limited"

    def __init__(self):
        self.calls = 0

    def extract_structured(self, **kw):
        self.calls += 1
        raise RateLimited("You've hit your session limit")


def test_rate_limit_defers_without_spending_attempts(db, monkeypatch):
    from salescoach import providers
    limited = Limited()
    providers.set_override(limited)
    try:
        call = paste.import_text(db, "Me: hello there.\nThem: hi, send the deck.", "Rate limited call")
        worker.drain(db)
    finally:
        providers.clear_override()
    ev = db.execute("SELECT status, attempts, error, updated_at FROM wf_events WHERE entity_id=?", (call,)).fetchone()
    assert ev["status"] == "pending" and ev["attempts"] == 0 and "rate limited" in ev["error"]
    assert bus.claim_next(db) is None                      # parked for later, not retried in a hot loop
    assert limited.calls == 1                              # one attempt, no fallback, no retry burst
    assert repo.get_call(db, call)["wf_state"] == "diarized"
