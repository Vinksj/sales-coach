"""What the shared machinery shows one user of another's work (Postgres, cloud mode).

  * The nav's "Working on <event type> <entity id>" named whatever the worker was doing, another rep's call id
    included, on every user's pages: it is shown only when the event is the viewer's own.
  * PREP_REQUESTED carried the meeting title and attendees the rep typed, in wf_events, which every session can
    read (store/rls.py: the bus carries ids only): it names a key of the rep's own user_state now.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from salescoach import identity
from salescoach.orchestrator import bus
from salescoach.schemas.events import Event
from salescoach.web import app as app_module

from test_route_crawl import A, B, ORIGIN, cloud, objects, two_reps  # noqa: F401

pytestmark = pytest.mark.postgres_only


@pytest.fixture
def app(db, objects, cloud):
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    app.state.gmail_factory = lambda: None
    return app


def test_the_nav_names_only_the_viewers_own_work(app, db, objects):
    with identity.as_user(db, A):
        event = Event(type="CALL_ENDED", entity_id=objects["call_id"], dedupe_key="nav:1")
        assert bus.publish(db, event)
        db.commit()
    app.state.worker = SimpleNamespace(current=event)
    client = TestClient(app, follow_redirects=False)
    mine = client.get("/", headers={"x-test-user": A, "accept": "text/html"}).text
    theirs = client.get("/", headers={"x-test-user": B, "accept": "text/html"}).text
    assert f"Working on CALL_ENDED {objects['call_id']}" in mine
    assert "Working on" not in theirs and objects["call_id"] not in theirs


def test_a_prep_request_puts_no_meeting_details_on_the_bus(app, db, objects, pg_owner):
    client = TestClient(app, follow_redirects=False)
    r = client.post(f"/deals/{objects['deal_id']}/prep", data={"title": "Board pricing call", "attendees": "cfo@acme.test"},
                    headers={"x-test-user": A, "accept": "text/html", **ORIGIN})
    assert r.status_code == 303
    payload = pg_owner.execute("SELECT payload FROM wf_events WHERE type='PREP_REQUESTED'").fetchone()[0]
    assert "Board pricing" not in payload and "cfo@acme.test" not in payload
    key = json.loads(payload)["request"]
    with identity.as_user(db, B):                                   # the details are A's own user_state
        assert db.execute("SELECT COUNT(*) FROM user_state WHERE key=?", (key,)).fetchone()[0] == 0
    with identity.as_user(db, A):
        assert "Board pricing call" in db.execute("SELECT value FROM user_state WHERE key=?", (key,)).fetchone()[0]
