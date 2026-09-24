"""Postgres as the store behind `salescoach serve` and `salescoach migrate`.

Boots the app with DATABASE_URL pointing at the test database (the schema the harness made for this
test) and hits /health and /; then drives `salescoach migrate --check` and `salescoach migrate`
against a schema of their own through the CLI, the way a deployment does.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from salescoach import cli
from salescoach.store import db, pgmigrate, stores
from salescoach.web.app import create_app

pytestmark = pytest.mark.postgres_only


def test_serve_boots_against_database_url(db):
    assert stores.db_path() == os.environ["DATABASE_URL"]
    app = create_app(start_worker=False, live_factory=None, hub=None)
    assert app.state.db_path is None                       # the URL comes from the environment
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["db"] == "ok"
    home = client.get("/", follow_redirects=True)
    assert home.status_code == 200
    conn = stores.sales()
    try:
        assert conn.dialect == "postgres"
        assert conn.execute("SELECT current_schema()").fetchone()[0].startswith("sc_test_")
    finally:
        conn.close()


def _url_in_schema(schema):
    return f"{os.environ['DATABASE_URL']}?options=-c%20search_path%3D{schema}"


def test_migrate_cli_applies_and_checks(capsys):
    schema = f"sc_test_cli_{uuid.uuid4().hex[:8]}"
    admin = db.connect(os.environ["DATABASE_URL"])
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        url = _url_in_schema(schema)
        assert cli.main(["migrate", "--check", "--url", url]) == 1          # empty schema: behind
        assert "BEHIND" in capsys.readouterr().out
        assert cli.main(["migrate", "--url", url]) == 0
        out = capsys.readouterr().out
        assert "applied 0001_baseline" in out and f"schema at version {pgmigrate.expected_version()}" in out
        assert cli.main(["migrate", "--check", "--url", url]) == 0
        assert "up to date" in capsys.readouterr().out
        assert cli.main(["migrate", "--url", url]) == 0                     # a second run applies nothing
        assert "nothing to apply" in capsys.readouterr().out
        conn = db.connect(url)
        try:
            assert conn.table_exists("calls") and conn.table_exists("schema_migrations")
            assert pgmigrate.check(conn) == (pgmigrate.expected_version(), pgmigrate.expected_version())
        finally:
            conn.close()
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def test_store_refuses_a_schema_behind_the_code():
    schema = f"sc_test_old_{uuid.uuid4().hex[:8]}"
    admin = db.connect(os.environ["DATABASE_URL"])
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        stores._pg_schema = schema                              # the harness's own slot, restored by it
        with pytest.raises(pgmigrate.SchemaOutOfDate, match="salescoach migrate"):
            stores.sales()
    finally:
        stores._pg_schema = None
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
