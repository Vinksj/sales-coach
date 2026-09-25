"""Phase D plugin: transcript sources (salescoach/sources).

What it attaches, through the plugin seam only:
  router            POST /import/file, POST /import/webhook, POST /calls/<id>/speaker. Built on
                    first access, so the CLI and the worker never import the web layer.
  register_cli      salescoach sources list | poll [KIND ...] | webhook-secret
  start_background  local: the `sources` scheduler duty: every minute it polls whichever enabled
                    adapters are due (the watched folder each minute, API adapters per their
                    poll_minutes). One adapter's failure is recorded against that adapter
                    (state sources:<kind>:last_error) and never stops the others.
                    cloud: the per-user `recorders` duty (Phase 4): every minute, for each active
                    user in their own service session, their own due recorder connections
                    (sources/connections.py; cursors and errors in each connection's row).
No tables of its own: calls.history and the rebuilt calls table are core migration 4.
Settings: the user's sources.yaml (salescoach.sources.save). Keys: config.set_secret.
"""
import json

POLL_TICK_S = 60


def __getattr__(name):
    if name == "router":
        from ..sources.web import router
        return router
    raise AttributeError(name)


def run_sources(conn) -> dict:
    from .. import sources
    results = sources.poll(conn)
    return {kind: ({"error": r["error"]} if "error" in r else
                   {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
            for kind, r in results.items()}


def run_recorders(conn) -> dict:
    """The per-user `recorders` duty (cloud): the bound user's own due recorder connections, each on its
    own (sources/connections.poll_user). Runs inside that user's service session (automation/scheduler)."""
    from ..sources import connections
    return connections.poll_user(conn)


def background_duties() -> list:
    """Local install: the org-level `sources` duty (the watched folder, the org's API keys), once, as the
    local user, exactly as before. Cloud: the per-user `recorders` duty instead: every active user's own
    connections in that user's session; the org-level poller does not exist there."""
    from .. import identity
    from ..automation.scheduler import Duty
    if identity.cloud():
        return [Duty("recorders", run_recorders, lambda: POLL_TICK_S, first_delay_s=80, per_user=True)]
    return [Duty("sources", run_sources, lambda: POLL_TICK_S, first_delay_s=75, per_user=False)]


def start_background(db_path, stop):
    from ..automation.scheduler import start
    start(db_path, stop, duties=background_duties())


# ---- CLI ----------------------------------------------------------------------------------------

def cmd_sources(args):
    from .. import sources
    from ..store import stores
    conn = stores.sales()
    if args.action == "list":
        for d in sources.catalog(conn):
            if d["mode"] == "export":
                print(f"{d['kind']:<10} export     {d['label']}: {d['how']}")
                continue
            state = "on " if d["enabled"] else "off"
            ready = "ready" if d["configured"] else (f"needs {d['api_key_env']}" if d["needs_key"] else "unavailable")
            every = f" every {d['poll_minutes']} min" if d["poll_minutes"] else ""
            note = "" if d["verified"] else "  [untested against the live API]"
            print(f"{d['kind']:<10} {state} {d['mode']:<5} {ready}{every}{note}")
            if d["last_error"]:
                print(f"{'':<10} last error {d['last_error'].get('at')}: {d['last_error'].get('error')}")
    elif args.action == "poll":
        print(json.dumps(sources.poll(conn, kinds=args.kinds or None, force=True), indent=1, default=str))
    elif args.action == "webhook-secret":
        print(sources.new_webhook_secret())
        print("Send it as the X-Salescoach-Secret header. It is not shown again; run this again to replace it.")


def register_cli(sub):
    p = sub.add_parser("sources", help="transcript sources: list them, poll them now, or create the webhook secret")
    p.add_argument("action", choices=["list", "poll", "webhook-secret"])
    p.add_argument("kinds", nargs="*", help="poll only these sources (folder, fireflies, fathom, granola)")
    p.set_defaults(fn=cmd_sources)
