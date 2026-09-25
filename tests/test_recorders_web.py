"""The rep's recorder pages and the per-connection webhook, through the app in cloud mode (Postgres).

Pinned: /me/setup shows a "Your call recorder" card per ALLOWED kind; Save stores the rep's own key
(encrypted, never rendered back, never in a redirect or flash) and Test says who the key belongs to;
Disconnect deletes it. Setup > "Where calls come from" is an allow-list in cloud mode: it asks for no
org key, refuses one posted anyway, and a disallowed kind disappears from the reps' cards. The webhook
per connection: a valid recorder signature (or the connection's own token) imports AS THE CONNECTION'S
OWNER whatever the payload claims; a wrong secret is 403; a disconnected or unknown connection is 404;
the org-level /import/webhook is 404 in cloud. /me/meetings shows upcoming / recorded, imported (a link) /
recorded, not imported yet / not recorded. Local mode keeps its org-level sources page and redirects
the per-rep routes there.
"""
import base64
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from salescoach import identity, users
from salescoach.sources import connections
from salescoach.sources.recorders import signatures
from salescoach.store import stores
from salescoach.web import app as app_module

import recorder_fakes as rf
from conftest import seed_org_settings
from test_tokens import K1, keys  # noqa: F401

ORIGIN = {"origin": "http://testserver"}
SIGNING = "whsec_" + base64.b64encode(b"fathom-signing-secret-24b").decode()


def _as_header(monkeypatch):
    from salescoach.web import auth

    def from_header(scope, headers):
        user_id = headers.get("x-test-user")
        if not user_id:
            return None
        with identity.activate(None):
            conn = stores.sales()
        try:
            with conn.as_system():
                row = users.get(conn, user_id)
        finally:
            conn.close()
        return (users.as_actor(row), None) if row else None
    monkeypatch.setattr(auth, "_cloud_actor", from_header)


@pytest.fixture
def fake(monkeypatch):
    fake = rf.FakeRecorders()
    monkeypatch.setattr(connections, "TRANSPORT", fake.transport())
    return fake


@pytest.fixture
def app(db, keys, fake, monkeypatch, request):
    """Reps A and B and an admin; the org set up; cloud mode; identity from a test header."""
    if db.dialect != "postgres":
        pytest.skip("cloud mode needs Postgres")
    for uid, name, role in (("u-a", "Asha Rao", "rep"), ("u-b", "Bala Krishnan", "rep"), ("u-admin", "Adi", "admin")):
        users.create(db, f"{uid[2:]}@tessel.test", name, role=role, user_id=uid)
    db.commit()
    seed_org_settings(request.getfixturevalue("pg_owner"))
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    for name in ("SALESCOACH_PASSWORD", "SALESCOACH_PASSWORD_HASH", "SALESCOACH_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    _as_header(monkeypatch)
    application = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    application.state.gmail_factory = lambda: None
    return TestClient(application, follow_redirects=False)


def as_(user):
    return {"x-test-user": user, "accept": "text/html"}


def post(client, user, url, data=None, **headers):
    return client.post(url, data=data or {}, headers={**as_(user), **ORIGIN, **headers})


def rows(pg_owner, sql="SELECT * FROM source_connections ORDER BY owner_id, kind", params=()):
    return [dict(r) for r in pg_owner.execute(sql, params).fetchall()]


def connect(client, user, kind, key):
    r = post(client, user, f"/me/recorders/{kind}/connect", {"api_key": key, "action": "save"})
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers.get("location")
    return r


# ---- the rep's card ---------------------------------------------------------------------------------

@pytest.mark.postgres_only
def test_a_rep_connects_their_own_recorder_and_the_key_never_comes_back(app, fake, pg_owner):
    fake.account("fireflies", "ff-SECRET-key-a", email="a@tessel.test", name="Asha Rao")
    page = app.get("/me/setup", headers=as_("u-a")).text
    assert 'id="recorders"' in page and all(f'id="rec-{k}"' in page for k in ("fathom", "fireflies", "tldv", "granola"))
    assert "untested against the live API" in page and "Settings &gt; Developer settings" in page
    r = post(app, "u-a", "/me/recorders/fireflies/connect", {"api_key": "ff-SECRET-key-a", "action": "test"})
    assert "The+Fireflies.ai+key+works" in r.headers["location"] and "a%40tessel.test" in r.headers["location"]
    assert rows(pg_owner) == []                                                   # Test saves nothing
    r = connect(app, "u-a", "fireflies", "ff-SECRET-key-a")
    assert "ff-SECRET-key-a" not in r.headers["location"]
    [row] = rows(pg_owner)
    assert row["owner_id"] == "u-a" and row["kind"] == "fireflies" and row["account_email"] == "a@tessel.test"
    assert "ff-SECRET-key-a" not in json.dumps(row, default=str)                  # ciphertext at rest
    page = app.get("/me/setup", headers=as_("u-a")).text
    assert "Connected as a@tessel.test" in page and "ff-SECRET-key-a" not in page and row["secret_enc"] not in page
    assert f"/me/connections/{row['id']}/poll" in page
    # B's page knows nothing of it
    page_b = app.get("/me/setup", headers=as_("u-b")).text
    assert row["id"] not in page_b and "Connected as" not in page_b
    # a refused key: said so, key not echoed, nothing stored for B
    r = post(app, "u-b", "/me/recorders/fireflies/connect", {"api_key": "bad-KEY-for-b", "action": "save"})
    assert "err=" in r.headers["location"] and "refused" in r.headers["location"] and "bad-KEY" not in r.headers["location"]
    assert [x["owner_id"] for x in rows(pg_owner)] == ["u-a"]
    # disconnect
    r = post(app, "u-a", f"/me/connections/{row['id']}/disconnect")
    assert r.status_code == 303
    [row] = rows(pg_owner)
    assert row["status"] == "disconnected" and row["secret_enc"] is None and row["key_id"] is None


@pytest.mark.postgres_only
def test_import_now_polls_that_connection_as_the_rep(app, fake, pg_owner):
    fake.account("fathom", "fk-a", meetings=[rf.fathom_meeting("rec-1", "a@tessel.test", "Asha Rao",
                                                               when=_iso(_now() - timedelta(hours=3)))])
    connect(app, "u-a", "fathom", "fk-a")
    [row] = rows(pg_owner)
    r = post(app, "u-a", f"/me/connections/{row['id']}/poll")
    assert "1+imported" in r.headers["location"], r.headers["location"]
    assert rows(pg_owner, "SELECT owner_id, source_ref FROM calls") == [{"owner_id": "u-a", "source_ref": "fathom:u-a:rec-1"}]
    assert post(app, "u-b", f"/me/connections/{row['id']}/poll").status_code == 404        # not B's


# ---- the admin's allow-list -----------------------------------------------------------------------

@pytest.mark.postgres_only
def test_setup_sources_in_cloud_is_an_allow_list_without_org_keys(app, fake, pg_owner):
    page = app.get("/setup/sources", headers=as_("u-admin")).text
    assert 'action="/setup/sources/allowed"' in page and 'name="api_key"' not in page
    assert "Gong, Otter, Avoma, Chorus" in page and "webhook/secret" not in page
    assert app.get("/setup/sources", headers=as_("u-a")).status_code == 403              # an admin's page
    r = post(app, "u-admin", "/setup/sources/fireflies", {"api_key": "ORG-WIDE-KEY", "enabled": "1"})
    assert "err=" in r.headers["location"]
    r = post(app, "u-admin", "/setup/sources/webhook/secret")
    assert "err=" in r.headers["location"]
    from salescoach import config
    assert not config.has_secret("FIREFLIES_API_KEY") and not config.has_secret("WEBHOOK_SECRET")
    r = post(app, "u-admin", "/setup/sources/allowed", {"kinds": ["fathom", "granola"]})
    assert r.status_code == 303 and "Fathom" in r.headers["location"]
    assert connections.allowed_kinds() == ["fathom", "granola"]
    page = app.get("/me/setup", headers=as_("u-a")).text
    assert 'id="rec-fathom"' in page and 'id="rec-granola"' in page
    assert 'id="rec-fireflies"' not in page and 'id="rec-tldv"' not in page
    r = post(app, "u-a", "/me/recorders/fireflies/connect", {"api_key": "k-123", "action": "save"})
    assert "not+allowed" in r.headers["location"] and rows(pg_owner) == []


# ---- the webhook per connection ------------------------------------------------------------------

def _signed(body: bytes, secret=SIGNING, msg="msg_1"):
    stamp = str(int(time.time()))
    return {"webhook-id": msg, "webhook-timestamp": stamp,
            "webhook-signature": signatures.standard_sign(secret, msg, stamp, body), "content-type": "application/json"}


@pytest.mark.postgres_only
def test_a_signed_push_imports_as_the_connections_owner_whatever_the_payload_claims(app, fake, pg_owner, monkeypatch):
    fake.account("fathom", "fk-a")
    connect(app, "u-a", "fathom", "fk-a")
    [row] = rows(pg_owner)
    r = post(app, "u-a", f"/me/connections/{row['id']}/signing-secret", {"secret": SIGNING})
    assert "Signing+secret+saved" in r.headers["location"]
    assert SIGNING not in json.dumps(rows(pg_owner), default=str)
    payload = {**rf.fathom_meeting("rec-42", "a@tessel.test", "Asha Rao"), "owner": "u-b", "owner_id": "u-b",
               "user_id": "u-b", "recorded_by": {"name": "Bala Krishnan", "email": "b@tessel.test"}}
    body = json.dumps(payload).encode()
    url = f"/import/webhook/{row['id']}"
    r = app.post(url, content=body, headers=_signed(body))                      # no session, no Origin
    assert r.status_code == 201, r.text
    call = rows(pg_owner, "SELECT node_id, owner_id, source_ref, history FROM calls")
    assert call == [{"node_id": r.json()["call_id"], "owner_id": "u-a", "source_ref": "fathom:u-a:rec-42", "history": 0}]
    assert app.post(url, content=body, headers=_signed(body, msg="msg_2")).json()["created"] is False
    assert app.post(url, content=body, headers=_signed(body, secret="whsec_" + base64.b64encode(b"x" * 25).decode())
                    ).status_code == 403
    assert app.post(url, content=body, headers={"content-type": "application/json"}).status_code == 403
    tampered = body.replace(b"rec-42", b"rec-43")
    assert app.post(url, content=tampered, headers=_signed(body)).status_code == 403
    assert app.post("/import/webhook/rc-" + "0" * 32, content=body, headers=_signed(body)).status_code == 404
    monkeypatch.setenv("WEBHOOK_SECRET", "an-org-secret")          # even with the org secret: no org webhook in cloud
    assert app.post("/import/webhook", content=body, headers={"x-salescoach-secret": "an-org-secret"}).status_code == 404
    post(app, "u-a", f"/me/connections/{row['id']}/disconnect")
    assert app.post(url, content=body, headers=_signed(body, msg="msg_3")).status_code == 404
    assert len(rows(pg_owner, "SELECT node_id FROM calls")) == 1


@pytest.mark.postgres_only
def test_a_token_push_fetches_the_meeting_with_the_owners_own_key(app, fake, pg_owner):
    meeting = rf.fireflies_transcript("ff-9", [("Asha Rao", "I will send it Friday."), ("Chen Wu", "Fine.")])
    fake.account("fireflies", "key-a", email="a@tessel.test", name="Asha Rao", meetings=[meeting])
    connect(app, "u-a", "fireflies", "key-a")
    [row] = rows(pg_owner)
    page = post(app, "u-a", f"/me/connections/{row['id']}/webhook-token")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    token = page.text.split('id="webhook-secret">')[1].split("<")[0]
    assert token.startswith("whk_") and f"/import/webhook/{row['id']}" in page.text
    assert token not in json.dumps(rows(pg_owner), default=str)                   # only its hash
    assert token not in app.get("/me/setup", headers=as_("u-a")).text              # shown once
    url = f"/import/webhook/{row['id']}"
    body = json.dumps({"meetingId": "ff-9", "eventType": "Transcription completed", "owner_id": "u-b"}).encode()
    assert app.post(url, content=body, headers={"x-salescoach-secret": "wrong"}).status_code == 403
    r = app.post(url, content=body, headers={"x-salescoach-secret": token})
    assert r.status_code == 201, r.text
    assert rows(pg_owner, "SELECT owner_id, source_ref FROM calls") == [{"owner_id": "u-a", "source_ref": "fireflies:u-a:ff-9"}]
    assert {k for (_, k, _, _, _) in fake.requests} == {"key-a"}                   # the owner's key, only
    assert app.post(url, content=b"not json", headers={"x-salescoach-secret": token}).status_code == 422
    fake.not_ready.add("ff-10")
    meeting10 = rf.fireflies_transcript("ff-10", [("Asha Rao", "x"), ("Chen Wu", "y")])
    fake.accounts[("fireflies", "key-a")]["meetings"].append(meeting10)
    r = app.post(url, content=json.dumps({"meetingId": "ff-10"}).encode(), headers={"x-salescoach-secret": token})
    assert r.status_code == 202
    [row] = rows(pg_owner)
    assert any(e["ext_id"] == "ff-10" and e["status"] == "pending" for e in json.loads(row["state"])["recent"])


# ---- My meetings ----------------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


@pytest.mark.postgres_only
def test_my_meetings_shows_the_four_statuses(app, fake, pg_owner, db):
    now = _now()
    t_imported, t_pending, t_missing = now - timedelta(hours=3), now - timedelta(hours=20), now - timedelta(days=3)
    fake.account("fathom", "fk-a", meetings=[
        rf.fathom_meeting("rec-in", "a@tessel.test", "Asha Rao", when=_iso(t_imported), title="Acme discovery"),
        rf.fathom_meeting("rec-wait", "a@tessel.test", "Asha Rao", when=_iso(t_pending), title="Beta pricing")])
    fake.not_ready.add("rec-wait")
    for rec in fake.accounts[("fathom", "fk-a")]["meetings"]:
        if rec["recording_id"] == "rec-wait":
            del rec["transcript"]                                                 # listed before its transcript
    with identity.as_user(db, "u-a"):
        for event_id, title, start in (("ev-up", "Gamma demo", now + timedelta(days=1)),
                                       ("ev-in", "Acme discovery", t_imported), ("ev-wait", "Beta pricing", t_pending),
                                       ("ev-miss", "Delta intro", t_missing)):
            db.execute("INSERT INTO calendar_meetings(event_id,title,start_at,end_at,attendees,first_seen_at) "
                       "VALUES (?,?,?,?,?,?)", (event_id, title, start.isoformat(), (start + timedelta(minutes=30)).isoformat(),
                                                json.dumps([{"email": "chen@buyer.example"}]), now.isoformat()))
        db.commit()
    connect(app, "u-a", "fathom", "fk-a")
    [row] = rows(pg_owner)
    post(app, "u-a", f"/me/connections/{row['id']}/poll", {"back": "meetings"})
    page = app.get("/me/meetings", headers=as_("u-a")).text
    call_id = rows(pg_owner, "SELECT node_id FROM calls")[0]["node_id"]
    for label in ("Upcoming", "Recorded, imported", "Recorded, not imported yet", "Not recorded"):
        assert label in page, label
    assert f'href="/calls/{call_id}"' in page and "Gamma demo" in page and "Delta intro" in page
    assert "not finished the transcript yet" in page
    assert "assign" not in page.lower().replace("nothing to assign or claim", "")
    page_b = app.get("/me/meetings", headers=as_("u-b")).text                       # B sees none of it
    assert "Acme discovery" not in page_b and "Gamma demo" not in page_b and call_id not in page_b
    assert 'href="/me/meetings"' in app.get("/", headers=as_("u-a")).text           # linked from Today


def test_local_mode_keeps_the_org_sources_and_sends_the_per_rep_routes_there(db, monkeypatch):
    monkeypatch.delenv(identity.MODE_ENV, raising=False)
    client = TestClient(app_module.create_app(start_worker=False, live_factory=None, hub=None), follow_redirects=False)
    page = client.get("/setup/sources").text
    assert 'name="api_key"' in page and "/setup/sources/allowed" not in page       # unchanged
    r = client.post("/me/recorders/fireflies/connect", data={"api_key": "k", "action": "save"},
                    headers={"origin": "http://testserver"})
    assert r.status_code == 303 and r.headers["location"] == "/setup/sources"
    assert client.post("/import/webhook/rc-" + "0" * 32, content=b"{}").status_code in (403, 404)
    assert db.execute("SELECT COUNT(*) FROM source_connections").fetchone()[0] == 0
