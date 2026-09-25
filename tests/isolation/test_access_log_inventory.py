"""The access log covers every read of someone else's work, decided in one place (Postgres, cloud mode).

manager/views.READ_PATHS and REP_PAGES say which GETs read one person's work; web/app.ReadLog logs every
successful one by a non-owner. Before, only /calls/{id} and /deals/{id} logged: a manager read a rep's runs,
live page, nudge timeline and JSON, follow-up nudges, deal intelligence and prep, Coach and Learning pages
without a record.

  * Inventory: every GET route that names an object in its path or takes ?rep= is covered by views.logs() or
    listed in EXEMPT with the reason. A new route fails here until it is placed deliberately.
  * Every covered page of rep A's, read by A's manager M, writes a row naming the right object and owner.
  * A reading their own pages, and B (who cannot read them) asking, log nothing.
"""
import inspect

import pytest
from fastapi.testclient import TestClient

from salescoach.manager import views
from salescoach.web import app as app_module

from test_route_crawl import _walk, cloud  # noqa: F401
from test_manager_review import A, B, M, _as, a_objects, get, org  # noqa: F401

pytestmark = pytest.mark.postgres_only

EXEMPT = {
    "/setup/method/custom/{key}": "an org methodology definition (admin only), nobody's work",
    "/deals": "the deal LIST (?rep= filters titles); opening a deal is logged",
}


def _get_routes(app):
    out = set()
    for r in _walk(app.routes):
        if "GET" not in (r.methods or ()):
            continue
        takes_rep = "rep" in inspect.signature(r.endpoint).parameters
        if "{" in r.path or takes_rep:
            out.add((r.path, takes_rep))
    return sorted(out)


def test_inventory_every_get_route_that_names_someones_work_is_logged_or_exempt(db, org, cloud):
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    routes = _get_routes(app)
    assert len(routes) >= 15, routes
    sample = lambda path: path.replace("{run_id}", "1").replace("{email_id}", "1").replace("{", "").replace("}", "")  # noqa: E731
    unplaced = [path for path, takes_rep in routes
                if path not in EXEMPT and not views.logs(sample(path), "someone" if takes_rep else None)]
    assert not unplaced, f"GET routes that read someone's work must be in manager/views.READ_PATHS/REP_PAGES " \
                         f"or EXEMPT here with a reason: {unplaced}"
    stale = set(EXEMPT) - {p for p, _ in routes}
    assert not stale, stale


@pytest.fixture
def client(db, org, a_objects, cloud, monkeypatch):
    monkeypatch.setattr(views, "THROTTLE_S", 0)                 # every read logs: one row per request to check
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    app.state.gmail_factory = lambda: None
    return TestClient(app, follow_redirects=False)


def _pages(o, run_call=None):
    call, deal = ("call", o["call_id"]), ("deal", o["deal_id"])
    return {
        f"/calls/{o['call_id']}": call, f"/calls/{o['call_id']}/runs": call, f"/runs/{o['run_id']}": ("call", run_call),
        f"/live/{o['call_id']}": call, f"/coach/live/{o['call_id']}": call, f"/coach/live/{o['call_id']}/nudges": call,
        f"/nudges/{o['nudge_email_id']}": ("email", str(o["nudge_email_id"])),
        f"/deals/{o['deal_id']}": deal, f"/deals/{o['deal_id']}/intel": deal, f"/deals/{o['deal_id']}/prep": deal,
        f"/deals/{o['deal_id']}/outcome": deal,
        f"/coach?rep={A}": ("coaching", A), f"/learning?rep={A}": ("coaching", A), f"/coach/intel?rep={A}": ("coaching", A),
    }


def _last(pg_owner):
    return pg_owner.execute("SELECT id, viewer_id, owner_user_id, entity_type, entity_id FROM access_log "
                            "ORDER BY id DESC LIMIT 1").fetchone()


def test_every_read_of_a_reps_work_by_the_manager_is_logged(client, a_objects, pg_owner):
    run_call = pg_owner.execute("SELECT call_id FROM agent_runs WHERE id=?", (a_objects["run_id"],)).fetchone()[0]
    for url, (kind, entity_id) in _pages(a_objects, run_call).items():            # a run is logged as its call
        before = _last(pg_owner)
        r = get(client, M, url)
        assert r.status_code == 200, (url, r.status_code)
        row = _last(pg_owner)
        assert row is not None and (before is None or row["id"] > before["id"]), f"{url}: nothing logged"
        assert (row["viewer_id"], row["owner_user_id"], row["entity_type"], row["entity_id"]) == (M, A, kind, entity_id), url
    # A sees who read their coaching pages
    page = get(client, A, "/learning").text
    assert "Viewed by" in page and "Mani V" in page
    assert "Viewed by" in get(client, A, "/coach").text


def test_the_owner_and_an_outsider_log_nothing(client, a_objects, pg_owner):
    for url in _pages(a_objects):
        get(client, A, url.replace(f"?rep={A}", ""))
        assert get(client, B, url).status_code == 404, url
    assert pg_owner.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 0
