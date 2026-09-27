"""Operator commands for a serverless deployment (salescoach/cli.py, docs/deploy-vercel.md).

  * `migrate` refuses a connection-pooler URL (its session advisory lock and SET would land on whichever server
    connection PgBouncer picks) before connecting;
  * `vercel-build` migrates only on a production build that opted in and has the owner URL; a preview build never;
  * `app-role` creates the serving role as the owner (no superuser, no BYPASSRLS, no CREATEDB, no CREATEROLE,
    CONNECT on the database), prints its URL once, leaves an existing fit role alone, refuses an unfit one, and
    refuses an "owner" that does not bypass row-level security (Postgres).
"""
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

import conftest
from salescoach import cli
from salescoach.store import db as dbmod

POOLED = "postgresql://neondb_owner:pw@ep-cool-123-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require"


def test_migrate_refuses_a_pooler_url_without_connecting(monkeypatch, capsys):
    monkeypatch.setattr(dbmod, "connect", lambda *a, **k: pytest.fail("connected through the pooler"))
    assert cli.main(["migrate", "--url", POOLED]) == 2
    err = capsys.readouterr().err
    assert "connection pooler" in err and "ep-cool-123-pooler" in err and "pw@" not in err


@pytest.mark.parametrize("env,flag,url,runs", [
    ("preview", "1", "postgresql://o@db/x", False),
    ("development", "1", "postgresql://o@db/x", False),
    ("production", "", "postgresql://o@db/x", False),
    ("production", "1", "", False),
    ("production", "1", "postgresql://o@db/x", True),
])
def test_vercel_build_migrates_only_an_opted_in_production_build(monkeypatch, capsys, env, flag, url, runs):
    calls = []
    monkeypatch.setattr(cli, "cmd_migrate", lambda args: calls.append(args.url) or 0)
    monkeypatch.setenv("VERCEL_ENV", env)
    monkeypatch.setenv("SALESCOACH_MIGRATE_ON_BUILD", flag)
    monkeypatch.setenv("DATABASE_MIGRATE_URL", url)
    assert cli.main(["vercel-build"]) == 0
    assert calls == ([url] if runs else [])
    assert ("skipped" in capsys.readouterr().out) is not runs


def test_vercel_build_fails_the_build_when_the_migration_fails(monkeypatch):
    monkeypatch.setattr(cli, "cmd_migrate", lambda args: 1)
    for name, value in (("VERCEL_ENV", "production"), ("SALESCOACH_MIGRATE_ON_BUILD", "1"),
                        ("DATABASE_MIGRATE_URL", "postgresql://o@db/x")):
        monkeypatch.setenv(name, value)
    assert cli.main(["vercel-build"]) == 1


def _url_as(name, password):
    parts = urlsplit(conftest.PG_URL)
    return urlunsplit((parts.scheme, f"{name}:{password}@{parts.hostname}:{parts.port}", parts.path, "", ""))


@pytest.fixture
def roles(pg_owner):
    made = []
    yield made
    dbname = pg_owner.execute("SELECT current_database()").fetchone()[0]
    for name in made:
        pg_owner.raw.execute(f'REVOKE CONNECT ON DATABASE "{dbname}" FROM "{name}"')
        pg_owner.raw.execute(f'DROP ROLE IF EXISTS "{name}"')


@pytest.mark.postgres_only
def test_app_role_creates_a_serving_role_and_prints_its_url(db, pg_owner, roles, monkeypatch, capsys):
    name = f"sc_app_{uuid.uuid4().hex[:8]}"
    roles.append(name)
    monkeypatch.setenv("DATABASE_MIGRATE_URL", conftest.PG_URL)
    assert cli.main(["app-role", "--name", name, "--vercel"]) == 0
    out = capsys.readouterr().out
    assert "BYPASSRLS yes" in out or "superuser yes" in out
    [line] = [x.strip() for x in out.splitlines() if x.strip().startswith("SALESCOACH_DATABASE_URL=")]
    url = line.split("=", 1)[1]
    assert urlsplit(url).username == name and urlsplit(url).hostname == urlsplit(conftest.PG_URL).hostname
    row = pg_owner.execute("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles "
                           "WHERE rolname = ?", (name,)).fetchone()
    assert tuple(row) == (False, False, False, False, True)
    conn = dbmod.connect(url)                                        # the printed URL logs in
    conn.system = True
    try:
        assert conn.execute("SELECT current_user").fetchone()[0] == name
    finally:
        conn.close()
    assert cli.serving_role_problems(url) == []                      # fit to serve: RLS applies to it
    assert cli.main(["app-role", "--name", name]) == 0               # again: checked, not recreated
    again = capsys.readouterr().out
    assert "fit to serve" in again and "DATABASE_URL=" not in again


@pytest.mark.postgres_only
def test_app_role_refuses_an_unfit_existing_role_and_an_owner_without_bypassrls(db, pg_owner, roles, monkeypatch,
                                                                              capsys):
    bad, weak = f"sc_bad_{uuid.uuid4().hex[:8]}", f"sc_weak_{uuid.uuid4().hex[:8]}"
    roles.extend([bad, weak])
    pg_owner.raw.execute(f'CREATE ROLE "{bad}" LOGIN PASSWORD \'x\' BYPASSRLS')
    pg_owner.raw.execute(f'CREATE ROLE "{weak}" LOGIN PASSWORD \'weak-owner-password\' CREATEROLE NOBYPASSRLS')
    dbname = pg_owner.execute("SELECT current_database()").fetchone()[0]
    pg_owner.raw.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO "{weak}"')
    assert cli.main(["app-role", "--name", bad, "--url", conftest.PG_URL]) == 2
    assert "row-level security would not bind it" in capsys.readouterr().err
    assert cli.main(["app-role", "--name", f"sc_x_{uuid.uuid4().hex[:6]}", "--url",
                     _url_as(weak, "weak-owner-password")]) == 2
    assert "does not bypass row-level security" in capsys.readouterr().err


def test_app_role_refuses_a_pooler_owner_url(capsys):
    assert cli.main(["app-role", "--url", POOLED]) == 2
    assert "connection pooler" in capsys.readouterr().err
