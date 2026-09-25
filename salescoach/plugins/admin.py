"""Phase 3 plugin: users, teams and invites for an admin (salescoach/adminui).

What it attaches through the plugin seam:
  NAV            the Admin entry (base.html shows it to admins only)
  router         /admin and its forms, built on first access (module __getattr__) so the CLI and the
                 worker never import the web layer
  register_cli   `salescoach tokens new-key` (a SALESCOACH_TOKEN_KEYS entry) and
                 `salescoach tokens rotate` (re-encrypt every OAuth grant under the newest key; on
                 Postgres explicitly as the owner role, DATABASE_MIGRATE_URL, or --as an active admin)
No tables of its own: users, teams, team_managers (users.py), invites, sessions and oauth_tokens
(schema-sales.sql) are core, because sign-in needs them with SALESCOACH_NO_PLUGINS=1 too.
"""
NAV = [("/admin", "Admin")]


def __getattr__(name):
    if name == "router":
        from ..adminui.web import router
        return router
    raise AttributeError(name)


ROTATE_WHO = ("`tokens rotate` reads and rewrites EVERY user's Google grant, which the row-level policy on "
              "oauth_tokens allows only the owner role or an active admin (store/rls.py): set DATABASE_MIGRATE_URL "
              "to the owner role's URL (as for `salescoach migrate`), or pass --as <the user id of an active admin>")


def _rotate_conn(args):
    """The connection `tokens rotate` runs on, chosen EXPLICITLY (it never guesses who it is):
      --as USER     identity.session(USER) through the app role; refused unless USER is an active admin
      Postgres      else the owner role (--url, else DATABASE_MIGRATE_URL), which bypasses row-level
                    security as the migrator does; refused when neither is given
      SQLite        the store as the local user (the only user, an admin; SQLite has no policies)
    Returns (context manager yielding the connection, None) or (None, refusal message)."""
    import contextlib
    import os
    from .. import identity
    from ..store import db, stores
    target = stores.db_path()
    on_postgres = isinstance(target, str) and db.is_postgres_url(target)
    if args.as_user:
        @contextlib.contextmanager
        def as_admin():
            with identity.session(args.as_user) as conn:
                if on_postgres and identity.actor_of(conn).role != "admin":
                    raise PermissionError(f"{args.as_user} is not an active admin. " + ROTATE_WHO)
                yield conn
        return as_admin(), None
    if on_postgres:
        url = args.url or os.environ.get("DATABASE_MIGRATE_URL")
        if not url or not db.is_postgres_url(url):
            return None, ROTATE_WHO

        @contextlib.contextmanager
        def as_owner():
            conn = db.connect(url)
            conn.system = True                                  # the owner role, as nobody: it bypasses RLS
            try:
                if stores._pg_schema:                           # the test harness's per-test schema
                    conn.execute(f'SET search_path TO "{stores._pg_schema}"')
                yield conn
            finally:
                conn.close()
        return as_owner(), None

    @contextlib.contextmanager
    def local():
        conn = stores.sales()
        try:
            yield conn
        finally:
            conn.close()
    return local(), None


def _cli_tokens(args):
    import sys
    from ..execution import tokens
    if args.action == "new-key":
        print(tokens.new_key_line(args.kid))
        print("# add it to the FRONT of SALESCOACH_TOKEN_KEYS (comma-separated), deploy, then `salescoach tokens rotate`",
              flush=True)
        return 0
    try:
        tokens.key_ring()
    except tokens.NoKeys as exc:
        print(f"tokens rotate: {exc}", file=sys.stderr)
        return 2
    opener, refusal = _rotate_conn(args)
    if refusal:
        print(f"tokens rotate: {refusal}", file=sys.stderr)
        return 2
    try:
        with opener as conn:
            report = tokens.rotate(conn)
    except PermissionError as exc:
        print(f"tokens rotate: {exc}", file=sys.stderr)
        return 2
    print(f"re-encrypted {report['rotated']} grant(s) under key {report['key']!r}; {report['skipped']} already on it")
    for item in report["unreadable"]:
        print(f"  could not read {item}: its key is no longer in SALESCOACH_TOKEN_KEYS (the user must reconnect)")
    return 1 if report["unreadable"] else 0


def register_cli(subparsers):
    t = subparsers.add_parser("tokens", help="the OAuth token key ring: new-key, rotate")
    t.add_argument("action", choices=("new-key", "rotate"))
    t.add_argument("--kid", help="new-key: the key id to use (default: a timestamp)")
    t.add_argument("--as", dest="as_user", metavar="USER_ID",
                   help="rotate: run as this active admin, through the app role (DATABASE_URL)")
    t.add_argument("--url", help="rotate: the owner role's postgresql:// URL (default: DATABASE_MIGRATE_URL)")
    t.set_defaults(fn=_cli_tokens)
