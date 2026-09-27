"""Serverless (salescoach/serverless.py): how a Vercel deployment is recognised and what it changes by itself.

  * the platform: SALESCOACH_PLATFORM wins; VERCEL=1 (set by Vercel in builds and functions) means vercel when it
    is unset; nothing set means a container, as before;
  * the cron credential: `Bearer <CRON_SECRET>` only, and nothing is authorised while the secret is unset or short;
  * files: DATA_DIR and RUNTIME_DIR default under /tmp on Vercel (the bundle is read-only), unchanged elsewhere;
  * the upload cap stays under the platform's 4.5 MB request body limit on Vercel, unchanged elsewhere;
  * the database: SALESCOACH_DATABASE_URL wins over DATABASE_URL (a Marketplace integration owns DATABASE_URL and
    fills it with the owner's URL); on Vercel the owner's variables are removed from the environment; a pooler URL
    (Neon's -pooler host) is recognised.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from salescoach import serverless
from salescoach.store import stores

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def clean_env(monkeypatch):
    for name in (serverless.PLATFORM_ENV, "VERCEL", serverless.CRON_SECRET_ENV, serverless.DATABASE_URL_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_the_platform_is_a_container_unless_vercel_says_otherwise(clean_env):
    assert serverless.platform() == "container" and not serverless.vercel()
    clean_env.setenv("VERCEL", "1")
    assert serverless.vercel()
    clean_env.setenv(serverless.PLATFORM_ENV, "container")              # an explicit choice wins over VERCEL=1
    assert not serverless.vercel()
    clean_env.delenv("VERCEL")
    clean_env.setenv(serverless.PLATFORM_ENV, "Vercel")
    assert serverless.vercel()


def test_the_cron_credential_is_the_bearer_secret_and_nothing_without_one(clean_env):
    assert not serverless.cron_authorized("Bearer ")
    assert not serverless.cron_authorized(None)
    clean_env.setenv(serverless.CRON_SECRET_ENV, "short")
    assert not serverless.cron_authorized("Bearer short")              # too short to be a secret: refused
    secret = "s3cret-for-the-cron-endpoints-0123456789"
    clean_env.setenv(serverless.CRON_SECRET_ENV, secret)
    assert serverless.cron_authorized(f"Bearer {secret}")
    for bad in (secret, f"Bearer {secret}x", f"bearer {secret}", f"Basic {secret}", "Bearer", ""):
        assert not serverless.cron_authorized(bad), bad


def test_files_default_under_tmp_on_vercel_only():
    code = "from salescoach import config; print(config.DATA_DIR); print(config.RUNTIME_DIR)"
    env = {k: v for k, v in os.environ.items()
           if k not in ("SALESCOACH_DATA", "SALESCOACH_RUNTIME", "SALESCOACH_PLATFORM", "VERCEL")}
    env["PYTHONPATH"] = str(ROOT)
    on = subprocess.run([sys.executable, "-c", code], env={**env, "VERCEL": "1"}, capture_output=True, text=True)
    assert on.stdout.split() == ["/tmp/salescoach/data", "/tmp/salescoach/runtime"], on.stderr
    off = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert off.stdout.split()[0] == str(ROOT / "data") and "/tmp/salescoach" not in off.stdout, off.stderr
    chosen = subprocess.run([sys.executable, "-c", code], env={**env, "VERCEL": "1", "SALESCOACH_DATA": "/x/data"},
                            capture_output=True, text=True)
    assert chosen.stdout.split()[0] == "/x/data"                        # an explicit setting always wins


def test_the_upload_cap_stays_under_the_platform_body_limit(clean_env):
    assert serverless.body_limit(20 * 1024 * 1024) == 20 * 1024 * 1024
    clean_env.setenv("VERCEL", "1")
    assert serverless.body_limit(20 * 1024 * 1024) < serverless.BODY_LIMIT_BYTES
    assert serverless.body_limit(1024) == 1024


def test_salescoach_database_url_wins_over_database_url(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql://owner@db.example/app")
    assert stores.db_path() == "postgresql://owner@db.example/app"
    clean_env.setenv(serverless.DATABASE_URL_ENV, "postgresql://salescoach_app@db.example/app")
    assert stores.db_path() == "postgresql://salescoach_app@db.example/app"


def test_on_vercel_the_owner_connection_details_leave_the_environment(clean_env):
    for name in ("DATABASE_URL_UNPOOLED", "PGPASSWORD", "POSTGRES_URL", "DATABASE_MIGRATE_URL"):
        clean_env.setenv(name, "postgresql://neondb_owner:pw@ep-x.example/neondb")
    clean_env.setenv("DATABASE_URL", "postgresql://neondb_owner:pw@ep-x-pooler.example/neondb")
    assert serverless.scrub_owner_env() == []                            # not on Vercel: nothing is touched
    assert os.environ["PGPASSWORD"]
    clean_env.setenv("VERCEL", "1")
    gone = serverless.scrub_owner_env()
    assert {"DATABASE_URL_UNPOOLED", "PGPASSWORD", "POSTGRES_URL", "DATABASE_MIGRATE_URL"} <= set(gone)
    assert "DATABASE_URL" in os.environ                                  # the only URL there is: kept, and checked
    clean_env.setenv(serverless.DATABASE_URL_ENV, "postgresql://salescoach_app:pw@ep-x.example/neondb")
    assert serverless.scrub_owner_env() == ["DATABASE_URL"]
    assert "DATABASE_URL" not in os.environ and not any(n in os.environ for n in gone)


def test_a_pooler_url_is_recognised():
    assert serverless.pooled_url("postgresql://u:p@ep-cool-name-123-pooler.us-east-2.aws.neon.tech/db?sslmode=require")
    assert not serverless.pooled_url("postgresql://u:p@ep-cool-name-123.us-east-2.aws.neon.tech/db?sslmode=require")
    assert not serverless.pooled_url("postgresql://u:p@db:5432/salescoach")
    problem = serverless.pooled_problem("postgresql://u:p@ep-a-pooler.x.neon.tech/db", "SALESCOACH_DATABASE_URL")
    assert "SALESCOACH_DATABASE_URL" in problem and "ep-a-pooler.x.neon.tech" in problem and ":p@" not in problem
