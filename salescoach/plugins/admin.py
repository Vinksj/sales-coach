"""Phase 3 plugin: users, teams and invites for an admin (salescoach/adminui).

What it attaches through the plugin seam:
  NAV            the Admin entry (base.html shows it to admins only)
  router         /admin and its forms, built on first access (module __getattr__) so the CLI and the
                 worker never import the web layer
  register_cli   `salescoach tokens new-key` (a SALESCOACH_TOKEN_KEYS entry) and
                 `salescoach tokens rotate` (re-encrypt every OAuth grant under the newest key)
No tables of its own: users, teams, team_managers (users.py), invites, sessions and oauth_tokens
(schema-sales.sql) are core, because sign-in needs them with SALESCOACH_NO_PLUGINS=1 too.
"""
NAV = [("/admin", "Admin")]


def __getattr__(name):
    if name == "router":
        from ..adminui.web import router
        return router
    raise AttributeError(name)


def _cli_tokens(args):
    from ..execution import tokens
    if args.action == "new-key":
        print(tokens.new_key_line(args.kid))
        print("# add it to the FRONT of SALESCOACH_TOKEN_KEYS (comma-separated), deploy, then `salescoach tokens rotate`",
              flush=True)
        return 0
    from ..store import stores
    conn = stores.sales()
    try:
        report = tokens.rotate(conn)
    finally:
        conn.close()
    print(f"re-encrypted {report['rotated']} grant(s) under key {report['key']!r}; {report['skipped']} already on it")
    for item in report["unreadable"]:
        print(f"  could not read {item}: its key is no longer in SALESCOACH_TOKEN_KEYS (the user must reconnect)")
    return 1 if report["unreadable"] else 0


def register_cli(subparsers):
    t = subparsers.add_parser("tokens", help="the OAuth token key ring: new-key, rotate")
    t.add_argument("action", choices=("new-key", "rotate"))
    t.add_argument("--kid", help="new-key: the key id to use (default: a timestamp)")
    t.set_defaults(fn=_cli_tokens)
