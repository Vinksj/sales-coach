"""Shared fixtures. Every test gets its own sales.db and never sees the real
world.db (the Jarvis bridge tests build their own copy).

Two backends. Without SALESCOACH_TEST_DATABASE_URL every test runs on SQLite exactly as before.
With it set to a postgresql:// URL, stores.sales() hands out pooled connections into a schema
created for the test the first time it opens the store (the baseline applied through
store/pgmigrate.py, the same path a deployment takes) and dropped afterwards; tests that never
touch the store cost nothing. `@pytest.mark.sqlite_only` skips a test on Postgres (say why in a
comment: migrations keyed on PRAGMA user_version, table rebuilds, file-level behaviour);
`@pytest.mark.postgres_only` skips it on SQLite. The `dialect` fixture says which one is running.

Every test also gets its own user-settings folder holding a CONFIGURED seller profile: the
seller this coach was first built for. The tracked prompts and config name nobody, so every
existing assertion about his name, company or languages in a prompt now passes only because
seller.render() put it there, which is the point.
"""
import os
import tempfile
import uuid
import warnings
from pathlib import Path

import pytest
import yaml

PG_URL = os.environ.get("SALESCOACH_TEST_DATABASE_URL") or None

SELLER = {
    "name": "Maya Iyer",
    "emails": ["maya@tessel.test", "maya@tesselops.test", "maya.iyer@gmail.com"],
    "company": "Tessel",
    "role": "Founder, CEO",
    "website": "www.tessel.test",
    "offering": ("AI agents that do the multi-party coordination work in freight and logistics operations "
                 "(payment follow-ups, invoice acknowledgement, carrier check-ins, load planning)"),
    "icp": "large Indian enterprises, mostly logistics companies",
    "buyer_titles": "promoters, MDs, CEOs, CFOs and COOs, not IT",
    "vocabulary": "plant, lane, dispatch, detention",
    "own_domains": ["tessel.test", "tesselops.test", "gmail.com"],
    "languages": ["en", "hi"],
    "timezone": "Asia/Kolkata",
    "signature": "Best,\nMaya\n\nMaya Iyer\nFounder, CEO\nTessel\nwww.tessel.test",
    "call_context": "Sales are founder-led. Deals are multi-stakeholder and slow.",
}


def write_seller(folder, profile=None) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "seller.yaml").write_text(yaml.safe_dump(SELLER if profile is None else profile, sort_keys=False))
    return folder


# Modules read the profile at import time too (common.IST in a test module's globals), before any
# fixture runs: point the settings at a throwaway folder first, so an import never sees a real one.
os.environ["SALESCOACH_SETTINGS"] = str(write_seller(Path(tempfile.mkdtemp(prefix="salescoach-test-settings-"))))

from salescoach import config, providers  # noqa: E402


@pytest.fixture(autouse=True)
def seller_settings(tmp_path, monkeypatch):
    """An isolated, configured settings folder per test. Tests that need another seller (or none)
    call write_seller(seller_settings, {...})."""
    folder = write_seller(tmp_path / "user-settings")
    monkeypatch.setenv("SALESCOACH_SETTINGS", str(folder))
    # No test may read a real secrets file: the legacy location points at nothing.
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "no-legacy-secrets.env")
    return folder


# ---- the store backend --------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: the test is about SQLite itself; skipped on Postgres")
    config.addinivalue_line("markers", "postgres_only: needs Postgres; skipped without SALESCOACH_TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    on_pg = pytest.mark.skip(reason="SQLite-only test; this run is on Postgres")
    no_pg = pytest.mark.skip(reason="Postgres-only test; set SALESCOACH_TEST_DATABASE_URL")
    for item in items:
        if PG_URL and "sqlite_only" in item.keywords:
            item.add_marker(on_pg)
        if not PG_URL and "postgres_only" in item.keywords:
            item.add_marker(no_pg)


def pytest_sessionstart(session):
    if PG_URL:
        _drop_leftover_schemas()


def pytest_sessionfinish(session, exitstatus):
    if PG_URL:
        from salescoach.store import stores
        stores.close_pools()


def _pg_admin():
    """A plain (unpooled) connection for schema housekeeping."""
    from salescoach.store import db
    return db.connect(PG_URL)


def _drop_leftover_schemas():
    conn = _pg_admin()
    try:
        names = [r[0] for r in conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'sc_test_%'").fetchall()]
        for name in names:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')
    finally:
        conn.close()


def _create_test_schema(url, name):
    from salescoach.store import db, pgmigrate, stores
    pool = stores._pool(url)
    conn = db.PostgresConnection(pool.getconn(timeout=30), release=pool.putconn)
    try:
        conn.execute(f'CREATE SCHEMA "{name}"')
        conn.execute(f'SET search_path TO "{name}"')
        pgmigrate.apply(conn)
    finally:
        conn.close()


def _drop_test_schema(name):
    conn = _pg_admin()
    try:
        conn.execute("SET lock_timeout = '5s'")
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    except Exception as exc:                # a thread left a transaction open: the session sweep gets it next run
        warnings.warn(f"could not drop test schema {name}: {exc}")
    finally:
        conn.close()


@pytest.fixture
def dialect():
    return "postgres" if PG_URL else "sqlite"


@pytest.fixture(autouse=True)
def store_backend(monkeypatch):
    """SQLite: DATABASE_URL is never inherited from the machine. Postgres: every stores.sales() in this
    test (any thread) lands in one fresh schema, made on first use and dropped at the end."""
    if not PG_URL:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        yield None
        return
    from salescoach.store import stores
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    made = {}

    def hook(url):
        name = f"sc_test_{uuid.uuid4().hex[:12]}"
        _create_test_schema(url, name)
        stores._pg_schema = made["schema"] = name
        return name

    monkeypatch.setattr(stores, "_pg_schema", None)
    monkeypatch.setattr(stores, "_pg_schema_hook", hook)
    monkeypatch.setattr(stores, "_pg_prepare", False)
    yield made
    if made:
        stores.forget_verified(PG_URL, made["schema"])
        _drop_test_schema(made["schema"])


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SALES_DB", str(tmp_path / "sales.db"))
    monkeypatch.setenv("SALESCOACH_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    from salescoach.store import stores
    monkeypatch.setattr(stores, "WORLD_DB", tmp_path / "absent-world.db")
    conn = stores.sales()
    yield conn
    conn.close()


@pytest.fixture
def fake_llm():
    from salescoach.providers.fake import FakeProvider
    fake = FakeProvider()
    providers.set_override(fake)
    yield fake
    providers.clear_override()
