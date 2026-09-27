"""The serverless e2e's one-shot `bootstrap` service: what docs/deploy-vercel.md tells a deployer to run from their
own machine before the first deploy, as the database OWNER (a Neon project's owner role; here the container's):

  1. `salescoach app-role --password-stdin --vercel`: creates salescoach_app (no superuser, no BYPASSRLS, owns
     nothing, CONNECT on the database) and prints SALESCOACH_DATABASE_URL (the compose file already holds it);
  2. `salescoach migrate`, then `salescoach migrate --check`, with the owner's DIRECT URL;
and the second, empty database the import-sqlite round trip fills (as e2e/migrate.py does for the container run).
"""
import os
import subprocess
import sys

import psycopg

OWNER = os.environ["DATABASE_MIGRATE_URL"]
SECOND = os.environ["IMPORT_MIGRATE_URL"]
APP_PASSWORD = os.environ["APP_ROLE_PASSWORD"]


def run(*args, stdin=None):
    done = subprocess.run(["salescoach", *args], input=stdin, text=True, capture_output=True)
    for line in (done.stdout + done.stderr).splitlines():
        if "postgresql://" not in line:                 # the printed app URL holds the password: not in the log
            print(f"bootstrap: {line}")
    if done.returncode:
        sys.exit(done.returncode)


run("app-role", "--password-stdin", "--vercel", stdin=APP_PASSWORD + "\n")
with psycopg.connect(OWNER, autocommit=True) as conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname = 'salescoach_import'").fetchone():
        conn.execute("CREATE DATABASE salescoach_import")
for url in (OWNER, SECOND):
    run("migrate", "--url", url)
    run("migrate", "--check", "--url", url)
print("bootstrap: app role created, both databases at this build's schema")
