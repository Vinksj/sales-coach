"""Local mode keeps its pages (the cloud-only fixes are tests/isolation/test_cloud_ui_polish.py), and the two
fixes that apply to both modes:

  * a pipeline step deferred by the daily model budget is recorded as waiting, "<step>: budget_deferred: <why>",
    with no exception class, and Today shows it in progress, not under Failed;
  * "Confirmed loops are mirrored to Jarvis" is said only while the Jarvis bridge is on.
"""
import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from salescoach import budget, repo
from salescoach.agents.base import AgentFailed
from salescoach.integrations import jarvis_bridge
from salescoach.orchestrator import workflow
from salescoach.web import app as app_module


@pytest.fixture
def client(db):
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    app.state.gmail_factory = lambda: None
    return TestClient(app, follow_redirects=False)


def get(client, url):
    r = client.get(url, headers={"accept": "text/html"})
    assert r.status_code == 200, (url, r.status_code)
    return r.text


def test_local_pages_keep_settings_live_capture_the_queue_pill_and_the_seller(client):
    today = get(client, "/")
    nav = today.split('<nav class="nav"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/setup"' in nav
    assert "Start a call" in today and "Start recording" in today
    assert "workflow events queued" in today
    review = get(client, "/setup/review")
    assert "You and your org" in review and "<dt>Seller</dt>" in review
    assert "Default: upload and folder" in review


def test_the_local_password_page_renders_as_before(db, monkeypatch):
    monkeypatch.setenv("SALESCOACH_PASSWORD", "a long local passphrase")
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    page = TestClient(app, follow_redirects=False).get("/login").text
    assert '<nav class="nav"' in page and "workflow events queued" in page


def _call(db, state="awaiting_review", error=None):
    call = repo.create_call(db, source="paste", title="A call", wf_state=state)
    if error:
        repo.update_call(db, call, wf_error=error)
    db.commit()
    return call


@pytest.mark.parametrize("on", (True, False))
def test_the_jarvis_sentence_follows_the_bridge(client, db, monkeypatch, on):
    monkeypatch.setattr(jarvis_bridge, "available", lambda: on)
    page = get(client, f"/calls/{_call(db)}")
    assert "Anything still proposed stays in Open Loops" in page
    assert ("Confirmed loops are mirrored to Jarvis." in page) is on


def test_a_budget_deferral_is_recorded_as_waiting_and_shown_in_progress(client, db, monkeypatch):
    workflow.ensure_plugins()
    at = workflow.STEP_NAMES.index("email_drafted")

    def over_budget(conn, call_id, force=False):
        try:
            raise budget.BudgetExceeded("user", 1.0, 2.02)
        except budget.BudgetExceeded as exc:
            raise AgentFailed(f"email deferred: {exc}", rate_limited=True) from exc

    pipeline = list(workflow.PIPELINE)
    pipeline[at] = ("email_drafted", over_budget)
    monkeypatch.setattr(workflow, "PIPELINE", pipeline)
    call = _call(db, state=workflow.STEP_NAMES[at - 1])
    with pytest.raises(AgentFailed):
        workflow.run_pipeline(db, call)
    error = repo.get_call(db, call)["wf_error"]
    assert error == "email_drafted: budget_deferred: the user model budget for today is used up: 2.02 of 1.00 USD"
    assert budget.is_waiting(error) and not budget.is_waiting("email_drafted: AgentFailed: boom")
    today = get(client, "/")
    assert str(escape(budget.WAITING_TEXT)) in today and "<h2>Failed</h2>" not in today and "AgentFailed" not in today
    page = get(client, f"/calls/{call}")
    assert str(escape(budget.WAITING_TEXT)) in page and "Pipeline failed" not in page
