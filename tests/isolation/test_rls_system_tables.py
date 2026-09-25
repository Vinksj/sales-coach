"""The SYSTEM tables Phases 3 and 6 added, under their row-level policies (Postgres only, the app role).

  sessions      open to the app role (the AuthGate finds a session before anyone is bound), which is safe
                because the table holds sha256 of the session id: the raw id is nowhere in it, and the
                hash is not a session id.
  invites       read and marked accepted by the Google callback with nobody bound; created and deleted
                by admins only.
  oauth_tokens  a user's own grant; any grant for an active admin; nothing for anyone else or for nobody.
                `salescoach tokens rotate` runs as the owner role or --as an admin, never as a rep.
  org_settings  read by any connection; written by an active admin only.
  state         the 'ops:%' heartbeat rows are nobody's; every other key is as before.
"""
import json
import os

import pytest
from psycopg import errors

from salescoach import cli, identity, sessions, users
from salescoach.execution import tokens
from salescoach.store import stores

pytestmark = pytest.mark.postgres_only

K1 = "k1:" + "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
K2 = "k2:" + "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="


@pytest.fixture
def org(db, monkeypatch):
    """Two reps and an admin who manages no team, made by the local admin; then cloud mode."""
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "a long random string for the tests")
    for uid, role in (("u-a", "rep"), ("u-b", "rep"), ("u-admin", "admin")):
        users.create(db, f"{uid}@tessel.test", uid, role=role, user_id=uid)
    db.commit()
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    return {uid: identity.Actor(uid, role=role) for uid, role in (("u-a", "rep"), ("u-b", "rep"), ("u-admin", "admin"))}


def _nobody():
    with identity.activate(None):
        conn = stores.sales()
    return conn


def _refused(conn, fn):
    with pytest.raises(errors.InsufficientPrivilege):
        fn()
    conn.rollback()


# ---- sessions --------------------------------------------------------------------------------------------

def test_sessions_hold_the_hash_and_work_with_nobody_bound(db, org, pg_owner):
    conn = _nobody()
    try:
        with conn.as_system():                          # the AuthGate's position: nobody is bound yet
            sid, cookie = sessions.create(conn, "u-a")
            other, _ = sessions.create(conn, "u-a")
            assert sessions.resolve(conn, sessions.session_id_from_cookie(cookie))["user_id"] == "u-a"
            assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (sid,)).fetchone()[0] == 0
            assert sessions.resolve(conn, sessions.key(sid)) is None          # a leaked hash opens nothing
    finally:
        conn.close()
    stored = [dict(r) for r in pg_owner.execute("SELECT * FROM sessions").fetchall()]  # every row, as the operator
    assert {r["id"] for r in stored} == {sessions.key(sid), sessions.key(other)}
    assert not any(sid in str(v) or other in str(v) for r in stored for v in r.values())
    with identity.as_actor(db, org["u-a"]):
        assert len(sessions.live_for(db, "u-a")) == 2
        assert sessions.revoke_all(db, "u-a", keep=sid) == 1
        assert sessions.resolve(db, sid) and sessions.resolve(db, other) is None
        assert sessions.revoke(db, sid) and sessions.live_for(db, "u-a") == []


# ---- invites -------------------------------------------------------------------------------------------------

def test_invites_are_read_and_accepted_with_nobody_bound_and_written_by_admins(db, org):
    from salescoach.adminui import ops
    with identity.as_actor(db, org["u-admin"]):
        ops.invite(db, "new@tessel.test", role="rep")
        db.commit()
    for who in ("u-a", None):                           # a rep, and nobody, cannot make or delete one
        with identity.as_actor(db, org.get(who)), db.as_system():
            _refused(db, lambda: db.execute(
                "INSERT INTO invites(email,user_id,invited_by,created_at) VALUES ('x@tessel.test','u-a','u-a','t')"))
            assert db.execute("DELETE FROM invites WHERE email='new@tessel.test'").rowcount == 0
            db.rollback()
    conn = _nobody()                                    # the Google callback, before anyone is bound
    try:
        with conn.as_system():
            row = conn.execute("SELECT * FROM invites WHERE email='new@tessel.test'").fetchone()
            assert row is not None and row["accepted_at"] is None
            assert conn.execute("UPDATE invites SET accepted_at='now' WHERE email=? AND accepted_at IS NULL",
                                ("new@tessel.test",)).rowcount == 1
            conn.commit()
    finally:
        conn.close()
    with identity.as_actor(db, org["u-admin"]):
        assert db.execute("DELETE FROM invites WHERE email='new@tessel.test'").rowcount == 1
        db.commit()


# ---- oauth_tokens ----------------------------------------------------------------------------------------------

def _grant(db, actor, monkeypatch):
    monkeypatch.setenv(tokens.KEYS_ENV, K1)
    with identity.as_actor(db, actor):
        tokens.store(db, actor.user_id, "1//rt-" + actor.user_id, ["openid"], f"{actor.user_id}@tessel.test")
        db.commit()


def test_oauth_tokens_are_the_users_own_or_an_admins(db, org, monkeypatch):
    _grant(db, org["u-a"], monkeypatch)
    with identity.as_actor(db, org["u-b"]):
        assert tokens.get(db, "u-a") is None                                   # B cannot read A's row ...
        assert db.execute("UPDATE oauth_tokens SET status='revoked' WHERE user_id='u-a'").rowcount == 0
        assert db.execute("DELETE FROM oauth_tokens WHERE user_id='u-a'").rowcount == 0
        _refused(db, lambda: db.execute(                                       # ... nor write one as A
            "INSERT INTO oauth_tokens(user_id,provider,key_id,created_at) VALUES ('u-a','other','k1','t')"))
    with identity.as_actor(db, org["u-a"]):
        assert tokens.refresh_token_of(db, "u-a") == "1//rt-u-a"               # A uses their own
    with identity.as_actor(db, org["u-admin"]):
        assert tokens.status_of(db, "u-a")["status"] == "active"               # the admin page's link status
        tokens.mark(db, "u-a", "revoked", "disabled by an admin")              # an admin disabling A
        db.commit()
    conn = _nobody()
    try:
        with conn.as_system():
            assert conn.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0   # nobody reads none
    finally:
        conn.close()
    with identity.as_actor(db, org["u-a"]):
        assert tokens.get(db, "u-a")["status"] == "revoked"


def test_tokens_rotate_runs_as_the_owner_role_or_an_admin_only(db, org, monkeypatch, capsys):
    for who in ("u-a", "u-b"):
        _grant(db, org[who], monkeypatch)
    monkeypatch.setenv(tokens.KEYS_ENV, f"{K2},{K1}")                           # k2 at the front: rotate onto it

    def key_ids():
        with identity.as_actor(db, org["u-admin"]):
            return sorted(r[0] for r in db.execute("SELECT key_id FROM oauth_tokens").fetchall())

    assert cli.main(["tokens", "rotate", "--as", "u-a"]) == 2                  # a rep: refused, nothing touched
    assert "not an active admin" in capsys.readouterr().err and key_ids() == ["k1", "k1"]
    owner_url = os.environ["DATABASE_MIGRATE_URL"]
    monkeypatch.delenv("DATABASE_MIGRATE_URL")
    assert cli.main(["tokens", "rotate"]) == 2                                 # no owner URL and no --as: refused
    assert "DATABASE_MIGRATE_URL" in capsys.readouterr().err and key_ids() == ["k1", "k1"]
    monkeypatch.setenv("DATABASE_MIGRATE_URL", owner_url)
    assert cli.main(["tokens", "rotate", "--as", "u-admin"]) == 0              # an admin sees every grant
    assert "re-encrypted 2 grant(s)" in capsys.readouterr().out and key_ids() == ["k2", "k2"]
    monkeypatch.setenv(tokens.KEYS_ENV, f"k3:{'AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM='},{K2}")
    assert cli.main(["tokens", "rotate"]) == 0                                 # the owner role (DATABASE_MIGRATE_URL)
    assert "re-encrypted 2 grant(s)" in capsys.readouterr().out and key_ids() == ["k3", "k3"]
    with identity.as_actor(db, org["u-b"]):
        assert tokens.refresh_token_of(db, "u-b") == "1//rt-u-b"               # still readable, by its owner


# ---- org_settings and the heartbeat rows ---------------------------------------------------------------------

def test_org_settings_read_by_anyone_written_by_admins(db, org):
    with identity.as_actor(db, org["u-admin"]):
        db.execute("INSERT INTO org_settings(name,body) VALUES ('seller', ?)", (json.dumps({"company": "Acme"}),))
        db.commit()
    with identity.as_actor(db, org["u-a"]):
        _refused(db, lambda: db.execute("INSERT INTO org_settings(name,body) VALUES ('policy','{}')"))
        assert db.execute("UPDATE org_settings SET body='{}' WHERE name='seller'").rowcount == 0
        assert db.execute("DELETE FROM org_settings WHERE name='seller'").rowcount == 0
        db.rollback()
    conn = _nobody()
    try:
        with conn.as_system():
            assert json.loads(conn.execute("SELECT body FROM org_settings WHERE name='seller'").fetchone()[0]) == {
                "company": "Acme"}
            assert conn.execute("UPDATE org_settings SET body='{}' WHERE name='seller'").rowcount == 0
    finally:
        conn.close()


def test_heartbeats_are_nobodys_and_nothing_else_in_state_is(db, org):
    from salescoach import ops
    with identity.as_actor(db, org["u-admin"]):
        db.execute("INSERT INTO state(key,value) VALUES ('poller:cursor','42')")
        db.commit()
    conn = _nobody()
    try:
        ops.write_heartbeat(conn, "worker", concurrency=2)
        beats = ops.read_heartbeats(conn)
        assert [b["concurrency"] for b in beats["workers"]] == [2] and not beats["workers"][0]["stale"]
        with conn.as_system():
            assert conn.execute("SELECT COUNT(*) FROM state WHERE key NOT LIKE 'ops:%'").fetchone()[0] == 0
            _refused(conn, lambda: conn.execute("INSERT INTO state(key,value) VALUES ('poller:cursor2','x')"))
            assert conn.execute("UPDATE state SET value='0' WHERE key='poller:cursor'").rowcount == 0
            assert conn.execute("DELETE FROM state WHERE key LIKE 'ops:%'").rowcount == 0
    finally:
        conn.close()
