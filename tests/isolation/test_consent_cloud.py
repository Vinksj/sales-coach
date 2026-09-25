"""The recording-consent notice in a cloud install (Phase 8, Postgres only): the admin sets it in Settings; a
rep sees it on every call page, on My meetings and as a Today card; a rep cannot change it."""
import uuid

import pytest
from fastapi.testclient import TestClient

from salescoach import identity, users
from salescoach.store.stores import now
from salescoach.web import app as app_module

from conftest import seed_org_settings
from test_route_crawl import ORIGIN, cloud  # noqa: F401

pytestmark = pytest.mark.postgres_only

R, D = "u-cr", "u-cd"
NOTICE = "Tell everyone the call is recorded for coaching, and stop if anyone objects."


@pytest.fixture
def client(db, cloud, pg_owner):  # noqa: F811
    users.create(db, "cr@tessel.test", "Rhea Rep", role="rep", user_id=R)
    users.create(db, "cd@tessel.test", "Dev Admin", role="admin", user_id=D)
    db.commit()
    seed_org_settings(pg_owner)
    with identity.as_actor(db, identity.Actor(R, identity.INTERACTIVE, "rep")):
        nid = f"call-{uuid.uuid4().hex[:10]}"
        db.execute("INSERT INTO nodes(id,type,title) VALUES (?,'call','Acme intro')", (nid,))
        db.execute("INSERT INTO calls(node_id,source,title,started_at,wf_state) VALUES (?,'paste','Acme intro',?,'done')",
                   (nid, now()))
        db.commit()
    c = TestClient(app_module.create_app(start_worker=False, live_factory=None, hub=None), follow_redirects=False)
    c.call_id = nid
    return c


def _h(who):
    return {"x-test-user": who, "accept": "text/html", **ORIGIN}


def test_the_admin_sets_it_and_every_rep_page_shows_it(client):
    r = client.post("/setup/compliance", data={"notice": NOTICE, "retention_days": "365"}, headers=_h(R))
    assert r.status_code == 403                                       # a rep cannot set it
    r = client.post("/setup/compliance", data={"notice": NOTICE, "retention_days": "365"}, headers=_h(D))
    assert r.status_code == 303
    call = client.get(f"/calls/{client.call_id}", headers=_h(R)).text
    assert NOTICE in call and "Recording consent" in call
    assert NOTICE in client.get("/me/meetings", headers=_h(R)).text
    today = client.get("/", headers=_h(R)).text
    assert "Your org asks you to tell participants calls are recorded" in today and NOTICE in today
    client.post("/setup/compliance", data={"notice": "", "retention_days": ""}, headers=_h(D))
    assert "Recording consent" not in client.get(f"/calls/{client.call_id}", headers=_h(R)).text
