"""The OWNER-role connection operator commands run on (import-sqlite; export --user, retention --dry-run
when no admin is named): DATABASE_MIGRATE_URL, as for `salescoach migrate`. It bypasses row-level
security, so it is chosen explicitly and never at runtime (docs/architecture.md, "Isolation")."""
import contextlib
import os

from ..store import db, stores

MIGRATE_URL_ENV = "DATABASE_MIGRATE_URL"


class NoOwnerURL(RuntimeError):
    pass


def owner_url(url=None) -> str:
    url = url or os.environ.get(MIGRATE_URL_ENV)
    if not url or not db.is_postgres_url(url):
        raise NoOwnerURL(f"needs the owner role's postgresql:// URL: set {MIGRATE_URL_ENV} (as for "
                         "`salescoach migrate`) or pass --url")
    return url


@contextlib.contextmanager
def connection(url=None):
    """An unpooled owner-role connection, marked system (nobody is bound), row_security off so that an owner
    role without BYPASSRLS fails loudly instead of silently seeing nothing. The test harness's per-test
    schema is honoured."""
    conn = db.connect(owner_url(url))
    conn.system = True
    try:
        if stores._pg_schema:
            conn.execute(f'SET search_path TO "{stores._pg_schema}"')
        conn.execute("SET row_security = off")
        yield conn
    finally:
        conn.close()
