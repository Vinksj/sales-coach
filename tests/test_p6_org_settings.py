"""Phase 6: org settings in the database (cloud mode).

config.load(name) overlays org_settings.body instead of the user_dir yaml, caches by version, and a
change made by another connection (another process) is seen on the next read with no restart;
save_user writes the row, never a file. Locally nothing changes. The setup wizard keeps working in
cloud mode on Postgres, its saves landing in the table. Under row-level security (store/rls.py) any
connection reads org_settings and only an active admin writes it, so every save here runs as one.
"""
import json

import pytest
from fastapi.testclient import TestClient

from salescoach import config, hosted, identity, seller, sessions, users
from salescoach.web.app import create_app


ADMIN = identity.Actor("u-admin", role="admin")


@pytest.fixture
def cloud(monkeypatch, db, dialect):
    if dialect == "postgres":                  # the admin who saves Settings (made by the local admin, before cloud)
        users.create(db, "u-admin@acme.test", "Ada Admin", role="admin", user_id="u-admin")
        db.commit()
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    config._org_cache.clear()
    yield db
    config._org_cache.clear()


def test_local_mode_keeps_the_files(db, seller_settings):
    assert config._org_store() is None
    path = config.save_user("policy", {"email_policy": "Z"})
    assert path == seller_settings / "policy.yaml" and config.load("policy")["email_policy"] == "Z"
    assert config.org_settings_versions() == {}


@pytest.mark.postgres_only
def test_cloud_mode_reads_and_writes_org_settings_not_files(cloud, seller_settings):
    db = cloud
    assert config._org_store() is not None
    # the file overlay is ignored: seller.yaml on disk says Tessel, the table says nothing yet
    assert config.load_user("seller") == {} and config.load("seller").get("company") in (None, "")
    assert not seller.org_configured()
    with identity.activate(ADMIN):
        assert config.save_user("seller", {"company": "Acme", "offering": "Widgets", "own_domains": ["acme.test"]}) is None
    assert not (seller_settings / "seller.yaml").read_text().startswith("company: Acme")   # no file written
    assert not list(seller_settings.glob("*.broken-*"))
    row = db.execute("SELECT body, version, updated_by FROM org_settings WHERE name='seller'").fetchone()
    assert json.loads(row["body"])["company"] == "Acme" and row["version"] == 1 and row["updated_by"] == "u-admin"
    assert config.load("seller")["company"] == "Acme" and config.load("seller")["own_domains"] == ["acme.test"]
    assert config.load_user("seller") == {"company": "Acme", "offering": "Widgets", "own_domains": ["acme.test"]}
    assert seller.org_configured()
    with pytest.raises(Exception, match="no actor bound|row-level security"):   # nobody bound: refused
        with identity.activate(None):
            config.save_user("seller", {"company": "Nobody's"})
    with identity.activate(identity.Actor("u-rep", role="rep")):        # not an admin: refused too
        with pytest.raises(Exception, match="row-level security"):
            config.save_user("seller", {"company": "A rep's"})
    with identity.activate(ADMIN):
        config.save_user("seller", {"company": "Acme Two", "offering": "Widgets"})
    assert config.org_settings_versions() == {"seller": 2}
    assert config.load("seller")["company"] == "Acme Two"
    assert config.user_problems() == []


@pytest.mark.postgres_only
def test_a_change_from_another_connection_is_seen_without_restart_and_the_cache_is_keyed_on_version(cloud, monkeypatch):
    db = cloud
    with identity.activate(ADMIN):
        config.save_user("policy", {"email_policy": "A"})
    assert config.load("policy")["email_policy"] == "A"
    fetched = []
    real = config._org_body

    def counting(url, name):
        fetched.append(name)
        return real(url, name)
    monkeypatch.setattr(config, "_org_body", counting)
    assert config.load("policy")["email_policy"] == "A"
    assert config.load("policy")["email_policy"] == "A"
    assert fetched == []                                   # same version: the cached merge, one version query each
    # "another process": a plain UPDATE on a different connection, bumping the version
    db.execute("UPDATE org_settings SET body=?, version=version+1 WHERE name='policy'", (json.dumps({"email_policy": "B"}),))
    db.commit()
    assert config.load("policy")["email_policy"] == "B"    # next read, no restart, no cache clear
    assert fetched == ["policy"]
    assert config.load("policy")["email_policy"] == "B" and fetched == ["policy"]
    # a row with the same version but a rewritten body is NOT seen: the version is the cache key by design
    db.execute("UPDATE org_settings SET body=? WHERE name='policy'", (json.dumps({"email_policy": "C"}),))
    db.commit()
    assert config.load("policy")["email_policy"] == "B"


@pytest.mark.postgres_only
def test_tracked_defaults_still_merge_under_the_row(cloud):
    with identity.activate(ADMIN):
        config.save_user("policy", {"email": {"account": "personal"}})
    merged = config.load("policy")
    tracked = config._read_yaml(str(config.CONFIG_DIR / "policy.yaml"))
    assert merged["email"]["account"] == "personal"
    for key in tracked:
        assert key in merged                               # nothing from the tracked file is lost


@pytest.mark.postgres_only
def test_setup_wizard_saves_land_in_org_settings_in_cloud_mode(cloud, monkeypatch, seller_settings):
    db = cloud
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "wizard-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    admin = users.create(db, "admin@acme.test", "Ada Admin", role="admin", extra_emails=[], timezone="Asia/Kolkata")
    rep = users.create(db, "rep@acme.test", "Ravi Rep", role="rep")
    db.commit()
    client = TestClient(create_app(start_worker=False, live_factory=None, hub=None), follow_redirects=False)
    _sid, cookie = sessions.create(db, rep["id"])                          # a rep: Settings are not theirs
    client.cookies.set(hosted.COOKIE, cookie)
    assert client.get("/setup").status_code == 403
    assert client.post("/setup/you", data={"company": "Rep Co", "offering": "x", "go": "next"},
                       headers={"origin": "http://testserver"}).status_code == 403
    _sid, cookie = sessions.create(db, admin["id"])                        # Google sign-in's server-side session
    client.cookies.set(hosted.COOKIE, cookie)
    origin = {"origin": "http://testserver"}
    assert client.get("/").status_code == 303 and client.get("/").headers["location"] == "/setup"   # org not set up
    r = client.get("/setup")
    assert r.status_code == 200
    r = client.post("/setup/you", data={"name": "Ada Admin", "emails": "admin@acme.test", "company": "Acme Cloud",
                                        "offering": "Agents for logistics", "own_domains": "acme.test",
                                        "website": "www.acme.test", "languages": "en", "timezone": "Asia/Kolkata",
                                        "go": "next"}, headers=origin)
    assert r.status_code in (200, 303), r.text[:300]
    body = db.execute("SELECT body FROM org_settings WHERE name='seller'").fetchone()
    assert body is not None and json.loads(body["body"])["company"] == "Acme Cloud"
    assert not (seller_settings / "seller.yaml").read_text().startswith("name: Ada")     # nothing on disk
    assert seller.org_configured()
    r = client.post("/setup/model/use", data={"provider": "anthropic", "heavy": "claude-opus-5",
                                              "light": "claude-sonnet-5", "go": "stay"}, headers=origin)
    assert r.status_code in (200, 303) and "err=" not in r.headers.get("location", ""), (r.headers.get("location"), r.text[:300])
    models = db.execute("SELECT body, version FROM org_settings WHERE name='models'").fetchone()
    assert models is not None and json.loads(models["body"])["provider"] == "anthropic"
    page = client.get("/setup/model").text
    assert "Model spend today" in page
    assert config.load("models")["provider"] == "anthropic"
