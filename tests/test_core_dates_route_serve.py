from datetime import date

from fastapi.testclient import TestClient

from salescoach import providers
from salescoach.agents.base import Agent
from salescoach.validators import voice_lint
from salescoach.validators.dates import weekday_mismatches

TODAY = date(2026, 9, 12)


def test_weekday_mismatches_both_orders():
    assert weekday_mismatches("Let's meet Tuesday 15 Sep at 11", TODAY) == []
    assert weekday_mismatches("Tue, 15th September works", TODAY) == []
    assert weekday_mismatches("Wednesday 16 Sep", TODAY) == []           # correct: 16 Sep 2026 is a Wednesday
    notes = weekday_mismatches("How about Monday Sep 15 or Thursday 16 Sep?", TODAY)
    assert len(notes) == 2 and "2026-09-15 is a Tuesday" in notes[0] and "2026-09-16 is a Wednesday" in notes[1]
    assert weekday_mismatches("Monday Jan 4", TODAY) == []          # 4 Jan 2027 is a Monday (next occurrence)
    assert weekday_mismatches("Mon 30 Feb, Sunday Morning", TODAY) == []    # impossible date / not a date


def test_email_lint_blocks_a_wrong_weekday(monkeypatch):
    monkeypatch.setattr("salescoach.validators.dates.today_ist", lambda: TODAY)
    issues = voice_lint.lint("NWP", "I'm free Monday 15 Sep at 11:00 IST.\n\nThanks")
    assert any(i.kind == "weekday" and i.severity == "block" for i in issues)
    assert not voice_lint.blocking(voice_lint.lint("NWP", "I'm free Tuesday 15 Sep at 11:00 IST.\n\nThanks"))


def test_agents_route_through_an_overridable_hook(fake_llm):
    class Custom(Agent):
        name = "custom"

        def route(self):
            return fake_llm, "chosen-model", "low"

    assert Custom().route() == (fake_llm, "chosen-model", "low")
    assert Agent.route.__qualname__ == "Agent.route"


def test_no_worker_preview_trusts_only_itself(db):
    from salescoach.web.app import create_app
    client = TestClient(create_app(start_worker=False, trusted_origins=set()))
    assert client.get("/").status_code == 200
    r = client.post("/deals", data={"name": "X"}, headers={"origin": "http://127.0.0.1:8140"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/deals", data={"name": "X"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code != 403
