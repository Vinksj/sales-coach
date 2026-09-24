"""The admin's page (plugins/admin.py, adminui/): users, teams, invites, and the audit trail.

Both backends: the page renders for the local admin, a team is created and renamed with an audit
row that names the actor, an invite on SQLite is refused with the reason, the ops refusals. Cloud
(Postgres): invite -> the person signs in; role and team edits; disable ends sessions and grants
at once and the person is out at the next request; enable; log out everywhere; managers per
team (a rep cannot be one); an admin cannot disable themselves or change their own role; a rep
and a manager get 404 on every /admin route; every write leaves an events row with actor_user_id.
"""
import pytest

from salescoach import identity, sessions, users
from salescoach.adminui import ops
from salescoach.execution import tokens
from test_google_auth import ORIGIN, client_for, cloud, fake, invite, sign_in  # noqa: F401


def audit_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT kind, actor_user_id, owner_id, before, after FROM events WHERE kind LIKE 'admin.%' ORDER BY id").fetchall()]


# ---- both backends, as the local admin -----------------------------------------------------------

def test_page_renders_for_the_local_admin_and_team_writes_are_audited(db):
    client = client_for()
    page = client.get("/admin")
    assert page.status_code == 200 and "People and teams" in page.text and "Maya Iyer" in page.text
    assert "grants no access to anyone's calls" in page.text and "single-user install" in page.text
    assert 'href="/admin"' in client.get("/", follow_redirects=True).text                # the nav shows it to an admin
    r = client.post("/admin/teams", data={"name": "West"}, headers=ORIGIN)
    assert r.status_code == 303 and "Team+West+created" in r.headers["location"]
    [team] = users.list_teams(db)
    r = client.post(f"/admin/teams/{team['id']}", data={"name": "West Coast"}, headers=ORIGIN)
    assert r.status_code == 303 and users.get_team(db, team["id"])["name"] == "West Coast"
    r = client.post("/admin/teams", data={"name": "west coast"}, headers=ORIGIN)
    assert "already+a+team" in r.headers["location"]
    rows = audit_rows(db)
    assert [(r["kind"], r["actor_user_id"], r["owner_id"]) for r in rows] == [
        ("admin.team.create", "local", "local"), ("admin.team.rename", "local", "local")]
    assert '"West Coast"' in rows[1]["after"] and '"West"' in rows[1]["before"]
    page = client.get("/admin")
    assert "West Coast" in page.text and 'value="West Coast"' in page.text


def test_invite_on_sqlite_is_refused_with_the_reason(db, dialect):
    if dialect != "sqlite":
        pytest.skip("the refusal is SQLite's")
    client = client_for()
    r = client.post("/admin/invite", data={"email": "asha@tessel.test"}, headers=ORIGIN)
    assert r.status_code == 303 and "needs+Postgres" in r.headers["location"]
    assert users.by_email(db, "asha@tessel.test") is None and audit_rows(db) == []
    assert 'disabled' in client.get("/admin").text                                      # the button says so too


def test_ops_refusals(db):
    with pytest.raises(ops.AdminError, match="not an email"):
        ops.invite(db, "nope")
    with pytest.raises(ops.AdminError, match="role must be"):
        ops.invite(db, "a@b.test", role="king")
    with pytest.raises(ops.AdminError, match="no such team"):
        ops.invite(db, "a@b.test", team_id="t-none")
    with pytest.raises(ops.AdminError, match="no such user"):
        ops.disable(db, "u-none")
    with pytest.raises(ops.AdminError, match="cannot disable yourself"):
        ops.disable(db, "local")
    with pytest.raises(ops.AdminError, match="own role"):
        ops.update_user(db, "local", role="rep")
    assert ops.update_user(db, "local", role="admin")["role"] == "admin"                # no change: no error, no audit
    with pytest.raises(ops.AdminError, match="needs a name"):
        ops.create_team(db, "  ")
    team = ops.create_team(db, "East")
    with pytest.raises(ops.AdminError, match="no such user"):
        ops.set_managers(db, team["id"], ["u-none"])
    assert ops.set_managers(db, team["id"], ["local"]) == ["local"]                     # the local admin may manage
    assert ops.set_managers(db, team["id"], ["local"]) == ["local"]                     # idempotent: one audit row
    assert [r["kind"] for r in audit_rows(db)] == ["admin.team.create", "admin.team.managers"]
    assert ops.enable(db, "local")["status"] == "active"                                 # not disabled: nothing to do
    assert ops.logout_everywhere(db, "local") == 0


# ---- cloud ---------------------------------------------------------------------------------------

pytestmark_cloud = pytest.mark.postgres_only


@pytest.fixture
def admin(cloud, fake, monkeypatch):
    """A signed-in admin (bootstrapped) and their client."""
    from salescoach import googleauth
    monkeypatch.setenv(googleauth.BOOTSTRAP_ENV, "adi@tessel.test")
    fake.identity = {"sub": "sub-adi", "email": "adi@tessel.test", "email_verified": True, "hd": "tessel.test",
                     "name": "Adi Admin"}
    client = client_for()
    assert sign_in(client, fake).status_code == 303
    return client, users.by_email(cloud, "adi@tessel.test")


def as_person(fake, email, name):
    fake.identity = {"sub": "sub-" + email, "email": email, "email_verified": True, "hd": "tessel.test", "name": name}


@pytestmark_cloud
def test_invite_edit_disable_enable_and_the_audit_trail(cloud, fake, admin):
    client, adi = admin
    r = client.post("/admin/invite", data={"email": "Asha@Tessel.test", "role": "rep", "name": "Asha"}, headers=ORIGIN)
    assert r.status_code == 303 and "can+sign+in+now" in r.headers["location"]
    asha = users.by_email(cloud, "asha@tessel.test")
    assert asha["status"] == "invited" and asha["role"] == "rep" and asha["name"] == "Asha"
    inv = cloud.execute("SELECT * FROM invites WHERE email='asha@tessel.test'").fetchone()
    assert inv["invited_by"] == adi["id"] and inv["accepted_at"] is None
    r = client.post("/admin/invite", data={"email": "asha@tessel.test"}, headers=ORIGIN)
    assert "already+on+the+list" in r.headers["location"]
    page = client.get("/admin")
    assert "asha@tessel.test" in page.text and ">invited<" in page.text and "never" in page.text
    # she signs in: the invite is taken up
    as_person(fake, "asha@tessel.test", "Asha Rao")
    hers = client_for()
    assert sign_in(hers, fake).status_code == 303
    assert users.get(cloud, asha["id"])["status"] == "active"
    assert cloud.execute("SELECT accepted_at FROM invites WHERE email='asha@tessel.test'").fetchone()[0]
    tokens.store(cloud, asha["id"], "1//rt-asha", ["https://www.googleapis.com/auth/gmail.compose"], "asha@tessel.test")
    cloud.commit()
    # role and team
    with identity.as_user(cloud, adi["id"]):
        team = ops.create_team(cloud, "West")
        cloud.commit()
    r = client.post(f"/admin/users/{asha['id']}", data={"role": "manager", "team_id": team["id"]}, headers=ORIGIN)
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    row = users.get(cloud, asha["id"])
    assert row["role"] == "manager" and row["team_id"] == team["id"]
    assert hers.get("/me/setup").status_code == 200                                     # still signed in; role read live
    # disable: out at once, sessions and grants gone
    r = client.post(f"/admin/users/{asha['id']}/disable", headers=ORIGIN)
    assert r.status_code == 303 and "signed+out+everywhere" in r.headers["location"]
    assert hers.get("/", headers={"accept": "text/html"}).status_code == 303
    assert sessions.live_for(cloud, asha["id"]) == [] and tokens.get(cloud, asha["id"])["status"] == "revoked"
    assert fake.revoked == [{"token": "1//rt-asha"}]
    assert sign_in(hers, fake).status_code == 403                                       # cannot sign in either
    page = client.get("/admin")
    assert ">disabled<" in page.text and f'action="/admin/users/{asha["id"]}/enable"' in page.text
    # enable: she signs in again; Google is hers to reconnect
    r = client.post(f"/admin/users/{asha['id']}/enable", headers=ORIGIN)
    assert r.status_code == 303 and users.get(cloud, asha["id"])["status"] == "active"
    assert sign_in(hers, fake).status_code == 303
    assert tokens.status_of(cloud, asha["id"])["status"] == "revoked"
    # log her out everywhere from here
    r = client.post(f"/admin/users/{asha['id']}/logout", headers=ORIGIN)
    assert "Signed+out+of+1+browser" in r.headers["location"] and sessions.live_for(cloud, asha["id"]) == []
    kinds = [(r["kind"], r["actor_user_id"], r["owner_id"]) for r in audit_rows(cloud)]
    assert kinds == [("admin.invite", adi["id"], adi["id"]), ("admin.team.create", adi["id"], adi["id"]),
                     ("admin.user.update", adi["id"], adi["id"]), ("admin.user.disable", adi["id"], adi["id"]),
                     ("admin.user.enable", adi["id"], adi["id"]), ("admin.user.logout", adi["id"], adi["id"])]
    disable_row = audit_rows(cloud)[3]
    assert '"sessions_revoked": 1' in disable_row["after"] and '"grants_revoked": 1' in disable_row["after"]
    assert '"status": "active"' in disable_row["before"]


@pytestmark_cloud
def test_managers_per_team_and_the_self_protections(cloud, fake, admin):
    client, adi = admin
    asha = invite(cloud, "asha@tessel.test", role="rep")
    mani = invite(cloud, "mani@tessel.test", role="manager")
    with identity.as_user(cloud, adi["id"]):
        team = ops.create_team(cloud, "West")
        cloud.commit()
    r = client.post(f"/admin/teams/{team['id']}/managers", data={"manager_id": [asha["id"]]}, headers=ORIGIN)
    assert "is+a+rep" in r.headers["location"] and users.managers_of(cloud, team["id"]) == []
    r = client.post(f"/admin/teams/{team['id']}/managers", data={"manager_id": [mani["id"], adi["id"]]},
                    headers=ORIGIN)
    assert "Managers+saved" in r.headers["location"]
    assert users.managers_of(cloud, team["id"]) == sorted([mani["id"], adi["id"]])
    page = client.get("/admin")
    assert f'value="{mani["id"]}" checked' in page.text and "manages 1 team" in page.text
    r = client.post(f"/admin/teams/{team['id']}/managers", data={}, headers=ORIGIN)
    assert users.managers_of(cloud, team["id"]) == []
    # the admin cannot lock themselves out
    r = client.post(f"/admin/users/{adi['id']}/disable", headers=ORIGIN)
    assert "cannot+disable+yourself" in r.headers["location"] and users.get(cloud, adi["id"])["status"] == "active"
    r = client.post(f"/admin/users/{adi['id']}", data={"role": "rep", "team_id": ""}, headers=ORIGIN)
    assert "own+role" in r.headers["location"] and users.get(cloud, adi["id"])["role"] == "admin"
    r = client.post(f"/admin/users/{adi['id']}", data={"role": "admin", "team_id": team["id"]}, headers=ORIGIN)
    assert "Saved" in r.headers["location"] and users.get(cloud, adi["id"])["team_id"] == team["id"]


@pytestmark_cloud
def test_non_admins_get_404_on_every_admin_route(cloud, fake, admin):
    _client, _adi = admin
    for email, role in (("asha@tessel.test", "rep"), ("mani@tessel.test", "manager")):
        invite(cloud, email, role=role)
        as_person(fake, email, email.split("@")[0])
        theirs = client_for()
        assert sign_in(theirs, fake).status_code == 303
        assert identity.cloud()
        assert theirs.get("/admin").status_code == 404
        assert 'href="/admin"' not in theirs.get("/", follow_redirects=True).text
        for path, data in [("/admin/invite", {"email": "x@tessel.test"}), ("/admin/teams", {"name": "Z"}),
                           (f"/admin/users/{_adi['id']}/disable", {})]:
            assert theirs.post(path, data=data, headers=ORIGIN).status_code == 404, (role, path)
    assert users.list_teams(cloud) == [] and users.get(cloud, _adi["id"])["status"] == "active"
    assert [r["kind"] for r in audit_rows(cloud)] == []
