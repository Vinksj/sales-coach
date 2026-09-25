"""The e2e stack's one-shot `migrate` service: what a deployer does before the first deploy
(docs/deploy-cloud.md "Launch checklist" 1), for the org database AND a second, empty database the
import-sqlite round trip fills. As the OWNER role: create the app role (no superuser, no BYPASSRLS),
grant it CONNECT, then `salescoach migrate` on each database."""
import os
import subprocess
import sys

import psycopg

OWNER = os.environ["DATABASE_MIGRATE_URL"]
SECOND = os.environ["IMPORT_MIGRATE_URL"]

with psycopg.connect(OWNER, autocommit=True) as conn:
    conn.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescoach_app') THEN
            CREATE ROLE salescoach_app LOGIN PASSWORD 'app-e2e' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        END IF; END $$""")
    conn.execute("GRANT CONNECT ON DATABASE salescoach TO salescoach_app")
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname = 'salescoach_import'").fetchone():
        conn.execute("CREATE DATABASE salescoach_import")
    conn.execute("GRANT CONNECT ON DATABASE salescoach_import TO salescoach_app")

for url in (OWNER, SECOND):
    done = subprocess.run(["salescoach", "migrate", "--url", url])
    if done.returncode:
        sys.exit(done.returncode)
    check = subprocess.run(["salescoach", "migrate", "--check", "--url", url])
    if check.returncode:
        sys.exit(check.returncode)
print("e2e migrate: both databases at this build's schema")
