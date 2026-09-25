"""The ActorGate in cloud mode (Postgres): who is bound, and in what order (web/app.py).

A request with no session cookie is answered before any store is opened (a page is sent to /login,
anything else gets 401); a session for a disabled user counts for nothing: it binds nobody and is
answered like no session (Phase 3's AuthGate resolves the server-side session and the users row on
every request; a disabled user lands on /login, where sign-in refuses them); a session for a user who
does not exist is no session; open paths pass with nobody bound and read the store as system; and a
route that runs a statement with nobody bound trips the suite's assertion (db.NoActorBound) instead
of silently reading nothing.
"""
import pytest
from fastapi.testclient import TestClient

from salescoach import hosted, identity, sessions, users
from salescoach.store import db as dbmod
from salescoach.store import stores
from salescoach.web import app as app_module
from conftest import seed_org_settings

pytestmark = pytest.mark.postgres_only


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    for name in ("SALESCOACH_PASSWORD", "SALESCOACH_PASSWORD_HASH", "SALESCOACH_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "test-secret")


@pytest.fixture
def people(db, monkeypatch):
    """The users and a server-side session for each, made in local mode before cloud mode is switched on."""
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "test-secret")
    users.create(db, "asha@tessel.test", "Asha Rao", role="rep", user_id="u-a")
    users.create(db, "gone@tessel.test", "Gone Person", role="rep", status="disabled", user_id="u-gone")
    db.commit()
    seed_org_settings(db)                          # cloud mode reads the org profile from org_settings
    cookies = {}
    for uid in ("u-a", "u-gone", "u-nobody"):
        if uid == "u-nobody":                      # a live session row whose user was never created
            sid = "no-such-user-session"
            cookies[uid] = sessions.cookie_value(sid)
            continue
        _sid, cookies[uid] = sessions.create(db, uid)
    return cookies


def _cookie(user_id, people):
    return {hosted.COOKIE: people[user_id]}


@pytest.fixture
def client(db, people, cloud):                     # the users are made in local mode, then cloud mode is switched on
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    return TestClient(app, follow_redirects=False)


def test_no_session_is_answered_before_any_store_read(client, monkeypatch):
    def no_store(*a, **k):
        raise AssertionError("the store was opened for a request with no session")
    monkeypatch.setattr(stores, "sales", no_store)
    page = client.get("/", headers={"accept": "text/html"})
    assert page.status_code == 303 and page.headers["location"].startswith("/login")
    other = client.get("/deals", headers={"accept": "text/html"})
    assert other.status_code == 303 and "next=%2Fdeals" in other.headers["location"]
    assert client.get("/loops.json").status_code == 401
    assert client.post("/calls/x/retry", headers={"origin": "http://127.0.0.1:8140"}).status_code == 401
    assert client.get("/static/app.css").status_code == 200                 # open, no store


def test_a_disabled_users_session_binds_nobody(client, people):
    r = client.get("/", headers={"accept": "text/html"}, cookies=_cookie("u-gone", people))
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/loops.json", cookies=_cookie("u-gone", people)).status_code == 401
    deals = client.get("/deals", headers={"accept": "text/html"}, cookies=_cookie("u-gone", people))
    assert deals.status_code == 303 and deals.headers["location"].startswith("/login")


def test_a_session_for_an_unknown_user_is_no_session(client, people):
    r = client.get("/", headers={"accept": "text/html"}, cookies=_cookie("u-nobody", people))
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/loops.json", cookies=_cookie("u-nobody", people)).status_code == 401


def test_an_active_user_is_bound_and_health_is_open(client, people):
    assert client.get("/", headers={"accept": "text/html"}, cookies=_cookie("u-a", people)).status_code == 200
    me = client.get("/me/setup", headers={"accept": "text/html"}, cookies=_cookie("u-a", people))
    assert me.status_code == 200 and "Asha Rao" in me.text
    health = client.get("/health")                                             # no session: a system read
    assert health.status_code == 200 and health.json()["db"] == "ok"


def test_a_route_that_reads_with_nobody_bound_trips_the_assertion(client, people):
    @client.app.get("/__test/no-actor")
    def no_actor():
        with identity.activate(None):
            conn = stores.sales()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return {"ok": True}

    @client.app.get("/__test/system-read")
    def system_read():
        with identity.activate(None):
            conn = stores.sales()
        try:
            with conn.as_system():
                return {"ok": conn.execute("SELECT 1").fetchone()[0]}
        finally:
            conn.close()

    with pytest.raises(dbmod.NoActorBound):
        client.get("/__test/no-actor", cookies=_cookie("u-a", people))
    assert client.get("/__test/system-read", cookies=_cookie("u-a", people)).json() == {"ok": 1}
