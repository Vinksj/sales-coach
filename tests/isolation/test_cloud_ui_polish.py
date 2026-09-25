"""What the cloud pages show a rep, a manager and an admin (Postgres, cloud mode): fixes from a browser pass.

  1. Settings in the nav linked every user to /setup, which answers 403 to anyone but an admin in cloud mode.
  2. The signed-out /login page showed the app nav and an "idle · worker off" pill.
  3. Today offered "Start a call" (live capture), which a cloud server never has.
  4. Today nagged reps and managers to finish the org's model setup, which only an admin can do.
  5. A step deferred by the daily model budget showed under Failed, as "AgentFailed: ...", "attempt 0 of 3".
  6. The call page said confirmed loops are mirrored to Jarvis; the bridge is off in cloud mode.
  7. /team's nine columns were wider than the page.
  8. /setup summarised the recorder step as "Default: upload and folder", and its review listed the admin's own
     name, email and timezone as if they were the org's.
Local mode renders as before: tests/test_ui_polish_local.py.
"""
import json

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from salescoach import budget, config, identity, users
from salescoach.orchestrator import bus
from salescoach.schemas.events import Event
from salescoach.sources import connections
from salescoach.web import app as app_module

import factories
from conftest import seed_org_settings
from test_route_crawl import cloud  # noqa: F401

pytestmark = pytest.mark.postgres_only

A, B, M, D = "u-a", "u-b", "u-m", "u-d"
ROLES = {A: "rep", B: "rep", M: "manager", D: "admin"}
WAITING = "email_drafted: budget_deferred: the user model budget for today is used up: 2.02 of 1.00 USD"


@pytest.fixture
def org(db):
    users.create_team(db, "West", team_id="t-west")
    for uid, role in ROLES.items():
        users.create(db, f"{uid}@tessel.test", f"Person {uid}", role=role,
                     team_id="t-west" if role == "rep" else None, user_id=uid)
    users.set_managers(db, "t-west", [M])
    db.commit()
    seed_org_settings(db)
    return True


@pytest.fixture
def client(db, org, cloud):  # noqa: F811
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    app.state.gmail_factory = lambda: None
    return TestClient(app, follow_redirects=False)


def get(client, who, url):
    r = client.get(url, headers={"x-test-user": who, "accept": "text/html"} if who else {"accept": "text/html"})
    assert r.status_code == 200, (who, url, r.status_code)
    return r.text


def _nav(html: str) -> str:
    return html.split('<nav class="nav"', 1)[1].split("</nav>", 1)[0] if '<nav class="nav"' in html else ""


def test_settings_is_in_the_nav_of_an_admin_only(client):
    assert 'href="/setup"' in _nav(get(client, D, "/"))
    for who in (A, M):
        page = get(client, who, "/")
        assert _nav(page) and 'href="/setup"' not in _nav(page)
        assert 'href="/me/setup"' in page                      # their own settings: You


def test_the_signed_out_login_page_has_no_app_nav_and_no_queue_pill(client):
    page = get(client, None, "/login")
    assert '<nav class="nav"' not in page and 'href="/loops"' not in page
    assert "worker off" not in page and "idle" not in page and "workflow events queued" not in page


def test_today_has_no_live_capture_panel_and_no_org_setup_nag_for_reps(client):
    for who in (A, M, D):
        page = get(client, who, "/")
        assert "Start a call" not in page and "Start recording" not in page and "Live capture is not available" not in page
    admin = get(client, D, "/")
    assert "Finish setting up" in admin                         # the model has not passed a test in this org
    for who in (A, M):
        assert "Finish setting up" not in get(client, who, "/")


@pytest.fixture
def waiting_call(db, org):
    with identity.as_actor(db, identity.Actor(A, role="rep")):
        call = factories.insert(db, "calls", A, factories.Owner(db, A))["node_id"]
        db.execute("UPDATE calls SET wf_state='loops_reconciled', wf_error=?, title='Budget call' WHERE node_id=?",
                   (WAITING, call))
        assert bus.publish(db, Event(type="PROCESS_CALL", entity_id=call, dedupe_key="budget:1",
                                     payload={"from": "email_drafted"}))
        db.commit()
    return call


def test_a_budget_deferral_waits_it_does_not_fail(client, waiting_call):
    today = get(client, A, "/")
    assert str(escape(budget.WAITING_TEXT)) in today and "Budget call" in today
    assert "<h2>Failed</h2>" not in today and "AgentFailed" not in today and "attempt 0 of 3" not in today
    call = get(client, A, f"/calls/{waiting_call}")
    assert str(escape(budget.WAITING_TEXT)) in call
    assert "Pipeline failed" not in call and "AgentFailed" not in call and "attempt 0" not in call
    assert "budget_deferred" not in call and "Retry from" not in call
    for who in (A, M):                                          # the rep's /calls and the manager's
        listing = get(client, who, "/calls")
        assert "Budget call" in listing and "Waiting for tomorrow&#39;s model budget" in listing
        assert "Budget call" not in get(client, who, "/calls?state=failed")
        assert "Budget call" in get(client, who, "/calls?state=processing")


def test_the_call_page_says_nothing_about_jarvis_in_cloud(client, db, org):
    with identity.as_actor(db, identity.Actor(A, role="rep")):
        call = factories.insert(db, "calls", A, factories.Owner(db, A))["node_id"]
        db.execute("UPDATE calls SET wf_state='awaiting_review' WHERE node_id=?", (call,))
        db.commit()
    page = get(client, A, f"/calls/{call}")
    assert "Anything still proposed stays in Open Loops" in page and "Jarvis" not in page


def test_the_team_table_fits_the_page(client):
    page = get(client, M, "/team")
    assert '<div class="table-wrap"><table class="table team-table">' in page
    css = (app_module.HERE / "static" / "app.css").read_text()
    assert ".table-wrap { overflow-x: auto; max-width: 100%; }" in css
    assert ".team-table thead th { white-space: normal;" in css             # headers wrap: the row fits


def test_setup_summarises_the_recorder_allow_list_and_reviews_org_fields_only(client, pg_owner):
    rail = get(client, D, "/setup/review")
    assert "All recorders allowed" in rail and "upload and folder" not in rail
    assert "Your org" in rail and "Tessel" in rail
    assert "<dt>Seller</dt>" not in rail and "<dt>Email</dt>" not in rail and "<dt>Timezone</dt>" not in rail
    assert f"{D}@tessel.test" not in rail and 'href="/me/setup"' in rail
    first = connections.catalog()[0]
    pg_owner.execute("INSERT INTO org_settings(name,body,version) VALUES ('sources',?,1)",
                     (json.dumps({"allowed_kinds": [first["kind"]]}),))
    pg_owner.commit()
    config._org_cache.clear()
    assert f"Allowed: {first['label']}" in get(client, D, "/setup/review")
