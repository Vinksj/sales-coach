"""Security review, finding 4 (startup half): `salescoach serve` in cloud mode refuses a DATABASE_URL role that
row-level security does not apply to: a superuser, a BYPASSRLS role, or one that owns the schema or its tables.
The app role passes. Postgres only.
"""
from urllib.parse import urlsplit, urlunsplit

import pytest

import conftest
from salescoach import cli, identity

pytestmark = pytest.mark.postgres_only


def _role_url(name):
    parts = urlsplit(conftest.PG_URL)
    return urlunsplit((parts.scheme, f"{name}:{name}@{parts.hostname}:{parts.port}", parts.path, "", ""))


def test_the_app_role_passes_and_the_owner_role_is_refused(db, pg_owner):
    assert cli.serving_role_problems(conftest.APP_URL) == []
    [problem] = cli.serving_role_problems(conftest.PG_URL)                # the test owner: a superuser, owns it all
    assert "is a superuser" in problem and "owns tables" in problem and "DATABASE_MIGRATE_URL" in problem


def test_a_bypassrls_role_and_a_member_of_the_owner_are_refused(db, pg_owner):
    owner = pg_owner.execute("SELECT current_user").fetchone()[0]
    pg_owner.raw.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescoach_bypass') THEN
          CREATE ROLE salescoach_bypass LOGIN PASSWORD 'salescoach_bypass' NOSUPERUSER BYPASSRLS;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescoach_ownerish') THEN
          CREATE ROLE salescoach_ownerish LOGIN PASSWORD 'salescoach_ownerish' NOSUPERUSER NOBYPASSRLS IN ROLE "{owner}";
        END IF; END $$""")
    dbname = pg_owner.execute("SELECT current_database()").fetchone()[0]
    pg_owner.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO salescoach_bypass, salescoach_ownerish')
    [problem] = cli.serving_role_problems(_role_url("salescoach_bypass"))
    assert "has BYPASSRLS" in problem and "superuser" not in problem
    [problem] = cli.serving_role_problems(_role_url("salescoach_ownerish"))
    assert "owns tables" in problem


def test_serve_in_cloud_mode_refuses_the_owner_role(db, pg_owner, monkeypatch, capsys):
    import uvicorn
    ran = []
    from salescoach import ops
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(ops, "run_worker", lambda **kw: ran.append("worker"))
    monkeypatch.setattr(cli, "cloud_problems", lambda role="all": [])     # the Google and key settings: not the point
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    monkeypatch.setenv("DATABASE_URL", conftest.PG_URL)
    assert cli.main(["serve", "--role", "worker"]) == 2
    assert "row-level security would not apply" in capsys.readouterr().err and ran == []
