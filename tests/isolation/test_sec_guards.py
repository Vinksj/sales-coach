"""Security review, findings 4, 5, 6 and 9: the database guards (Postgres only, generated store/rls.py).

  4  trg_users_guard and trg_comments_guard bind every role row security applies to, not the role NAMED
     salescoach_app: a login role that is a member of it (salescoach_web) cannot promote itself either.
  5  a user cannot change their own sign-in address (users.email) or Google account (google_sub), neither from
     /me/setup (the address stays; the form edits the other addresses) nor by SQL as themselves.
  6  an admin reads, revokes, re-encrypts and deletes another user's grant, but never inserts one, re-activates
     it, widens its scopes or swaps its token under the same key; the grant's own user keeps full control.
  9  every SECURITY DEFINER function runs with search_path = <its schema>, pg_temp: a temporary `users` table
     created by the app role does not change who the policies think the actor is.
"""
from urllib.parse import urlsplit, urlunsplit

import pytest
from psycopg import errors

import conftest
from salescoach import identity
from salescoach.adminui import ops
from salescoach.execution import tokens
from salescoach.store import db as dbmod
from test_route_crawl import A, B, ORIGIN, client, cloud, objects, two_reps  # noqa: F401

pytestmark = pytest.mark.postgres_only

K1 = "k1:" + "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
K2 = "k2:" + "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="


def _schema(pg_owner):
    return pg_owner.execute("SELECT current_schema()").fetchone()[0]


def _raw(url, schema, user_id, mode="interactive"):
    """An unpooled connection (closed by the caller: a temp table or a role never leaks into the pool)."""
    conn = dbmod.connect(url)
    conn.system = True
    conn.execute(f'SET search_path TO "{schema}"')
    conn.raw.execute("SELECT set_config('app.user_id', %s, false), set_config('app.mode', %s, false)", (user_id, mode))
    return conn


# ---- 4: the guards key on row security, not on a role name ------------------------------------------------

def _web_role_url(pg_owner):
    pg_owner.raw.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescoach_web') THEN
          CREATE ROLE salescoach_web LOGIN PASSWORD 'salescoach_web' NOSUPERUSER NOBYPASSRLS IN ROLE salescoach_app;
        END IF; END $$""")
    parts = urlsplit(conftest.PG_URL)
    return urlunsplit((parts.scheme, f"salescoach_web:salescoach_web@{parts.hostname}:{parts.port}", parts.path, "", ""))


def test_a_rep_under_another_login_role_cannot_promote_themselves(db, two_reps, pg_owner):
    conn = _raw(_web_role_url(pg_owner), _schema(pg_owner), A)
    try:
        with pytest.raises(errors.InsufficientPrivilege, match="only an admin"):
            conn.raw.execute("UPDATE users SET role='admin' WHERE id=%s", (A,))
        conn.raw.rollback()
        with pytest.raises(errors.InsufficientPrivilege, match="only an admin"):
            conn.raw.execute("UPDATE users SET email='ceo@tessel.test' WHERE id=%s", (A,))
    finally:
        conn.close()
    assert pg_owner.execute("SELECT role, email FROM users WHERE id=?", (A,)).fetchone() == ("rep", f"{A}@tessel.test")


def test_the_comment_guard_binds_another_login_role_too(db, objects, pg_owner):
    comment_id = objects["comment_id"]
    conn = _raw(_web_role_url(pg_owner), _schema(pg_owner), A)
    try:
        with pytest.raises(errors.InsufficientPrivilege, match="keeps its thread"):
            conn.raw.execute("UPDATE comments SET created_at='2000-01-01' WHERE id=%s", (comment_id,))
    finally:
        conn.close()


# ---- 5: the sign-in address and the Google account are an admin's ---------------------------------------

def test_me_setup_keeps_the_sign_in_address(client, pg_owner, db):
    r = client.post("/me/setup", data={"name": "Asha Rao", "emails": "ceo@tessel.test", "timezone": "Asia/Kolkata"},
                    headers={"x-test-user": A, "accept": "text/html", **ORIGIN})
    assert r.status_code == 303, r.text
    email, extra = pg_owner.execute("SELECT email, extra_emails FROM users WHERE id=?", (A,)).fetchone()
    assert email == f"{A}@tessel.test" and "ceo@tessel.test" in extra      # an extra address, not the sign-in one
    with identity.as_actor(db, identity.Actor(B, role="admin")):
        pg_owner.execute("UPDATE users SET role='admin' WHERE id=?", (B,))
        pg_owner.commit()
        assert ops.invite(db, "ceo@tessel.test", role="admin")["email"] == "ceo@tessel.test"   # nothing squatted
        db.commit()


def test_a_user_cannot_rewrite_their_own_address_or_google_account_by_sql(db, two_reps, pg_owner):
    pg_owner.execute("UPDATE users SET google_sub='sub-a' WHERE id=?", (A,))
    pg_owner.commit()
    with identity.as_actor(db, identity.Actor(A, role="rep")):
        for sql in ("UPDATE users SET email='b2@tessel.test' WHERE id=?", "UPDATE users SET google_sub='sub-x' WHERE id=?"):
            with pytest.raises(dbmod.Error, match="only an admin"):
                db.execute(sql, (A,))
            db.rollback()
        db.execute("UPDATE users SET name='Asha R' WHERE id=?", (A,))       # the rest of the profile is theirs
        db.commit()
    assert pg_owner.execute("SELECT email, google_sub, name FROM users WHERE id=?", (A,)).fetchone() == (
        f"{A}@tessel.test", "sub-a", "Asha R")


# ---- 6: an admin revokes, re-encrypts or deletes another user's grant, nothing else ---------------------

def test_an_admin_may_only_revoke_reencrypt_or_delete_anothers_grant(db, two_reps, pg_owner, monkeypatch):
    monkeypatch.setenv(tokens.KEYS_ENV, K1)
    pg_owner.execute("UPDATE users SET role='admin' WHERE id=?", (B,))
    pg_owner.commit()
    rep, admin = identity.Actor(A, role="rep"), identity.Actor(B, role="admin")
    with identity.as_actor(db, rep):
        tokens.store(db, A, "1//rt-a", ["openid"], "u-a@tessel.test", access_token="at-a", expires_in=3600)
        db.commit()
    with identity.as_actor(db, admin):
        for sql in ("UPDATE oauth_tokens SET scopes='[\"https://mail.google.com/\"]' WHERE user_id=?",
                    "UPDATE oauth_tokens SET email='x@tessel.test' WHERE user_id=?",
                    "UPDATE oauth_tokens SET refresh_token_enc='forged' WHERE user_id=?"):
            with pytest.raises(dbmod.Error, match="an admin may only"):
                db.execute(sql, (A,))
            db.rollback()
        with pytest.raises(dbmod.Error):
            db.execute("INSERT INTO oauth_tokens(user_id,provider,key_id,created_at) VALUES (?,'other','k1','t')", (A,))
        db.rollback()
        tokens.mark(db, A, "revoked", "disabled by an admin")                # revoking: allowed
        db.commit()
        with pytest.raises(dbmod.Error, match="an admin may only"):          # ... re-activating: not
            db.execute("UPDATE oauth_tokens SET status='active' WHERE user_id=?", (A,))
        db.rollback()
        monkeypatch.setenv(tokens.KEYS_ENV, f"{K2},{K1}")
        assert tokens.rotate(db)["rotated"] == 1                              # re-encrypting: allowed
    with identity.as_actor(db, rep):
        assert tokens.get(db, A)["key_id"] == "k2"
        tokens.store(db, A, "1//rt-a2", ["email"], "u-a@tessel.test")        # the user: full control of their own
        db.commit()
        assert tokens.refresh_token_of(db, A) == "1//rt-a2"
    with identity.as_actor(db, admin):
        assert tokens.delete(db, A)                                           # deleting: allowed
        db.commit()


# ---- 9: SECURITY DEFINER functions ignore the caller's search_path ----------------------------------------

def test_security_definer_functions_pin_their_search_path(db, two_reps, pg_owner):
    schema = _schema(pg_owner)
    rows = pg_owner.execute(
        "SELECT p.proname, p.proconfig FROM pg_proc p WHERE p.pronamespace = current_schema()::regnamespace "
        "AND (p.prosecdef OR p.prorettype = 'trigger'::regtype)").fetchall()
    names = {r[0] for r in rows}
    assert {"app_actor_role", "app_visible_owners", "app_offboard", "app_child_owner", "app_users_guard"} <= names
    for name, config in rows:
        assert config and f"search_path={schema}, pg_temp" in config, (name, config)


def test_a_temporary_users_table_does_not_make_a_rep_an_admin(db, two_reps, pg_owner):
    conn = _raw(conftest.APP_URL, _schema(pg_owner), A)
    try:
        conn.raw.execute("CREATE TEMP TABLE users (id text, status text, role text, team_id text)")
        conn.raw.execute("INSERT INTO pg_temp.users VALUES (%s, 'active', 'admin', NULL)", (A,))
        assert conn.raw.execute("SELECT app_actor_role()").fetchone()[0] == "rep"
        assert conn.raw.execute("SELECT app_actor_active()").fetchone()[0] is True
    finally:
        conn.close()
