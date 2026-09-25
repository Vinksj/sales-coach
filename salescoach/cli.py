"""salescoach command line.

  salescoach serve                        web UI + workflow worker on 127.0.0.1:8140
  salescoach serve --host 0.0.0.0         hosted: needs SALESCOACH_PASSWORD (docs/deploy.md)
  salescoach serve --role web|worker|scheduler   one image, three processes (docs/deploy-cloud.md)
  salescoach health                       the container health check for whatever role this is
  salescoach payloads export              raw payloads (cloud mode) to files, for support
  salescoach password-hash                a SALESCOACH_PASSWORD_HASH for the password on stdin
  salescoach tokens new-key|rotate        the OAuth token key ring (plugins/admin.py; docs/deploy-cloud.md)
  salescoach call --title "NWP weekly"    capture a call in the foreground; Ctrl-C ends it
  salescoach import-audio FILE ...        process an existing recording
  salescoach import-text FILE ...         process a text transcript (Granola export, paste)
  salescoach import-file FILE ...         a transcript exported by any recorder (.txt .vtt .srt .json)
  salescoach sources list|poll            recorder integrations (plugins/sources.py)
  salescoach process CALL [--from STEP]   (re)run the post-call pipeline synchronously
  salescoach work                         drain pending workflow events
  salescoach loops [--deal D] [--owner me|prospect] [--status open]
  salescoach deal add|list, person add
  salescoach models pull REPO             explicit model download (never implicit)
  salescoach jarvis sync|bootstrap
  salescoach import-sqlite PATH --as EMAIL [--dry-run]   a local install into an empty cloud org
  salescoach export --user EMAIL [--out FILE]  one user's own data, a zip of JSON files (admins)
  salescoach retention [--dry-run]        what the org's retention.days would delete (or deletes it)
  salescoach status
"""
import argparse
import json
import os
import signal
import sys
import threading

from . import repo
from .store import stores


def _conn():
    return stores.sales()


UNAUTHENTICATED_BIND = (
    "refusing to start: --host {host} would serve the coach to the network with no login.\n"
    "Set SALESCOACH_PASSWORD (or SALESCOACH_PASSWORD_HASH) to put it behind a password (see docs/deploy.md),\n"
    "bind to 127.0.0.1, or pass --allow-unauthenticated if you really mean it.")


def _loopback(host: str) -> bool:
    import ipaddress
    host = (host or "").strip().lower()
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cloud_problems(role: str = "all") -> list:
    """What a cloud process cannot start without: the session secret (a process that serves HTTP: role
    web or all), the Google client and the token keys (every role: a worker refreshes and uses the reps'
    grants)."""
    from . import googleauth, hosted
    from .execution import tokens
    out = []
    if role in ("all", "web") and not os.environ.get(hosted.SESSION_SECRET_ENV):
        out.append(f"{hosted.SESSION_SECRET_ENV} is not set (a long random string; it signs the session cookie)")
    out.extend(googleauth.problems())
    if not tokens.keys_configured():
        out.append(f"{tokens.KEYS_ENV} is not set (make one with `salescoach tokens new-key`)")
    return out


def cmd_serve(args):
    from . import hosted, identity
    raw_mode = (os.environ.get(identity.MODE_ENV) or "local").strip().lower()
    if raw_mode not in identity.MODES:
        print(f"{identity.MODE_ENV}={raw_mode!r} is not one of {', '.join(identity.MODES)}", file=sys.stderr)
        return 2
    if identity.cloud():
        from .store import db, stores
        if not db.is_postgres_url(str(stores.db_path())):
            print(stores.CLOUD_NEEDS_POSTGRES, file=sys.stderr)
            return 2
        missing = cloud_problems(args.role)
        if missing:
            print("refusing to start in cloud mode:\n  " + "\n  ".join(missing) + "\n(see docs/deploy-cloud.md)",
                  file=sys.stderr)
            return 2
    if args.role in ("all", "web") and not _loopback(args.host) and not hosted.auth_enabled() and not args.allow_unauthenticated:
        print(UNAUTHENTICATED_BIND.format(host=args.host), file=sys.stderr)
        return 2
    from . import ops
    role = args.role
    if role in ("worker", "scheduler"):
        # No HTTP here: the process runs its loops until SIGTERM and the container health check reads
        # its heartbeat (ops.check_health). A worker needs no port; a scheduler needs no password.
        if role == "worker":
            ops.run_worker(n=args.concurrency or None)
        else:
            ops.run_scheduler()
        return 0
    import uvicorn
    from .web.app import create_app
    if args.no_worker:
        # A read-mostly preview: no worker, no scheduler, no Jarvis sync, and only this origin trusted.
        app = create_app(start_worker=False, trusted_origins=set(), role="web" if role == "web" else "all")
    else:
        app = create_app(role=role, heartbeats=(role == "all"))
    options = {}
    trusted = hosted.trusted_proxies()
    if trusted:
        # Hosted: the platform's proxy sets X-Forwarded-For/-Proto; uvicorn rewrites the client address and
        # the scheme from them, so the login rate limit counts real addresses and the Secure cookie fits.
        options.update(proxy_headers=True, forwarded_allow_ips=trusted)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", **options)


def cmd_health(args):
    """Exit 0 when this process's role is healthy: HTTP /health for web/all, a fresh heartbeat row for a
    worker or a scheduler (they serve no HTTP). The Dockerfile's HEALTHCHECK runs this."""
    from . import ops
    ok, why = ops.check_health(args.role, port=args.port)
    print(f"{args.role}: {'ok' if ok else 'NOT OK'} ({why})", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cmd_payloads(args):
    """`payloads export`: every raw payload the store holds (cloud mode writes them; a local install keeps
    files under data/inbox instead) to <out>/<kind>/<id>.<ext>, or as JSON lines on stdout with --json."""
    from . import identity
    from .sources import base
    if args.as_user:
        with identity.session(args.as_user, mode=identity.SERVICE) as conn:
            return base.export_payloads(conn, args.out, since=args.since, as_json=args.json, out=sys.stdout)
    return base.export_payloads(_conn(), args.out, since=args.since, as_json=args.json, out=sys.stdout)


def cmd_password_hash(args):
    """Print a SALESCOACH_PASSWORD_HASH value for the password read from stdin (never an argument:
    arguments show in the process list and the shell history)."""
    from . import hosted
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        print("password-hash: read the password from stdin, e.g.  printf '%s' 'the password' | salescoach password-hash",
              file=sys.stderr)
        return 2
    print(hosted.make_hash(password))


def cmd_call(args):
    from .live.manager import LiveManager
    manager = LiveManager()
    call_id = manager.start_call(args.title, deal_id=args.deal, lang_mode=args.lang,
                                 participants=args.participant or ())
    print(f"recording {call_id}; Ctrl-C to stop", file=sys.stderr)
    stop = threading.Event()
    # SIGTERM as well as Ctrl-C: without it the default action kills us outright,
    # so stop_call never runs — the live ASR is never drained (the last utterance
    # is lost) and the archive is left as raw .pcm with the call stuck at 'live'.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    manager.stop_call(call_id)
    print(call_id)
    if args.process:
        cmd_process(argparse.Namespace(call_id=call_id, from_step=None, force=False))


def cmd_import_audio(args):
    from .sources.audio_file import import_audio
    conn = _conn()
    call_id = import_audio(conn, args.path, args.title, deal_id=args.deal, lang_mode=args.lang, layout=args.layout)
    for pid in args.participant or ():
        repo.add_participant(conn, call_id, pid)
    conn.commit()
    print(call_id)


def cmd_import_text(args):
    from .sources.paste import import_text
    text = sys.stdin.read() if args.path == "-" else open(args.path).read()
    conn = _conn()
    print(import_text(conn, text, args.title, deal_id=args.deal, started_at=args.date,
                      lang_mode=args.lang, participants=args.participant or ()))


def cmd_import_file(args):
    """A transcript file in any format the parsers read. --me names your speaker label when it is not
    your profile name ("none" = you do not speak in it); --history imports it as old history."""
    from pathlib import Path
    from .sources import base
    from .sources.adapters.upload import UploadAdapter
    path = Path(args.path).expanduser()
    conn = _conn()
    try:
        nt = UploadAdapter().normalize(path.read_bytes(), path.name, args.title)
        me = base.NOT_PRESENT if (args.me or "").lower() == "none" else args.me
        outcome = base.import_normalized(conn, nt, deal_id=args.deal or base.guess_deal(conn, nt),
                                         history=args.history, lang_mode=args.lang, me_label=me, add_me=True)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    print(outcome.call_id)
    if not outcome.created:
        print("already imported: this is the existing call", file=sys.stderr)
    elif outcome.needs_speaker:
        print("waiting: the coach cannot tell which speaker is you, and will not guess. Labels: "
              + ", ".join(outcome.labels) + "\nAnswer on the call page, or run this command again with "
              "--me \"<label>\" (or --me none). Nothing is processed until then.", file=sys.stderr)


def cmd_process(args):
    from .orchestrator import workflow
    conn = _conn()
    state = workflow.run_pipeline(conn, args.call_id, from_step=args.from_step, force=args.force)
    print(json.dumps({"call_id": args.call_id, "state": state}))


def cmd_work(args):
    from .orchestrator.worker import drain
    print(f"handled {drain(_conn())} events")


def cmd_loops(args):
    conn = _conn()
    where, params = ["1=1"], []
    if args.deal:
        where.append("l.deal_id=?")
        params.append(args.deal)
    if args.owner:
        where.append("l.owner=?")
        params.append(args.owner)
    if args.status != "all":
        where.append("l.status IN ('open','waiting')" if args.status == "open" else "l.status=?")
        if args.status != "open":
            params.append(args.status)
    rows = conn.execute(
        f"SELECT l.*, d.name AS deal FROM loops l LEFT JOIN deals d ON d.node_id=l.deal_id "
        f"WHERE {' AND '.join(where)} AND l.review_state!='rejected' "
        f"ORDER BY l.due_date IS NULL, l.due_date, l.priority", params).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=1))
        return
    for r in rows:
        print(f"{r['node_id']}  {r['due_date'] or '----------'}  {r['priority']:<8} {r['owner']:<8} "
              f"{r['status']:<8} {r['review_state']:<9} [{r['deal'] or '-'}] {r['description']}")


def cmd_deal(args):
    conn = _conn()
    if args.action == "add":
        if not args.name:
            print("deal add needs a name, e.g. salescoach deal add \"Acme pilot\" --account ...",
                  file=sys.stderr)
            return 2
        account = repo.find_account_by_domain(conn, args.domain[0]) if args.domain else None
        if account is None:
            account = repo.create_account(conn, args.account or args.name, args.domain or ())
        deal_id = repo.create_deal(conn, args.name, account_id=account, stage=args.stage)
        repo.link_deal_person(conn, deal_id, repo.ensure_me(conn), role="seller")
        conn.commit()
        print(deal_id)
    else:
        for r in conn.execute("SELECT d.node_id, d.name, d.stage, d.status, a.name AS account FROM deals d "
                              "LEFT JOIN accounts a ON a.node_id=d.account_id ORDER BY d.name"):
            print(f"{r['node_id']}  {r['name']}  [{r['account'] or '-'}]  {r['stage'] or '-'}  {r['status']}")


def cmd_person(args):
    conn = _conn()
    account = None
    if args.email and "@" in args.email:
        account = repo.find_account_by_domain(conn, args.email.split("@", 1)[1])
    pid = repo.find_person_by_email(conn, args.email) or repo.create_person(
        conn, args.name, email=args.email, account_id=account, title=args.title, contact_file=args.contact_file)
    if args.deal:
        repo.link_deal_person(conn, args.deal, pid, role=args.role)
    conn.commit()
    print(pid)


def cmd_models(args):
    from .speech import models
    if args.action == "pull-diarization":
        from .speech import diarize
        print(json.dumps(diarize.pull_models(), indent=1))
        return
    if args.action == "pull":
        if not args.repo:
            print("usage: salescoach models pull <huggingface repo>")
            return 1
        print(models.pull(args.repo))
    else:
        missing = set(models.missing_models())
        for repo_id in models.required_models():
            print(f"{'MISSING ' if repo_id in missing else 'ok      '}{repo_id}")


def cmd_jarvis(args):
    from .integrations import jarvis_bridge
    conn = _conn()
    if args.action == "sync":
        print(json.dumps(jarvis_bridge.sync(conn), indent=1))
    elif args.action == "bootstrap":
        print(json.dumps(jarvis_bridge.bootstrap_import(conn, args.deal, dry_run=args.dry_run), indent=1))
    conn.commit()


def cmd_onboard(args):
    from . import onboard
    print(json.dumps(onboard.run(_conn(), args.file), indent=1))


def cmd_backfill(args):
    from . import onboard
    results = onboard.backfill_granola(_conn(), args.range, only_deals=not args.all, dry_run=args.dry_run)
    for r in results:
        print(json.dumps(r))


def cmd_eval(args):
    from . import evals
    names = args.cases or evals.cases()
    if not names:
        print(f"no eval cases in {evals.eval_dir()}")
        return 1
    for name in names:
        print(evals.report(evals.run_case(name)))


def cmd_status(args):
    conn = _conn()
    for r in conn.execute("SELECT node_id, title, wf_state, wf_error, started_at FROM calls "
                          "ORDER BY started_at DESC LIMIT 15"):
        err = f"  ERROR: {r['wf_error']}" if r["wf_error"] else ""
        print(f"{r['node_id']}  {r['wf_state']:<18} {r['title'] or ''}{err}")
    pending = conn.execute("SELECT type, entity_id, status, attempts, error FROM wf_events "
                           "WHERE status!='done' ORDER BY id").fetchall()
    for e in pending:
        print(f"event {e['type']} {e['entity_id']} {e['status']} x{e['attempts']} {e['error'] or ''}")
    from .orchestrator import bus
    depth = bus.queue_depth(conn)
    if depth:
        print("queue by owner:")
        for d in depth:
            print(f"  {d['owner'] or '(none)':<16} pending {d['pending']:<4} running {d['running']:<3} failed {d['failed']:<3}"
                  + (f" oldest pending {d['oldest_pending']}" if d["oldest_pending"] else ""))
    if any(e["status"] in ("pending", "running") for e in pending) and not _worker_listening():
        print("note: events are queued but no worker is running on this database; start `salescoach serve` "
              "or run `salescoach work` to process them", file=sys.stderr)


def cmd_migrate(args):
    """Bring the Postgres schema up to this build (or, with --check, say whether it is)."""
    import os
    from .store import db, pgmigrate, stores
    url = args.url or os.environ.get("DATABASE_MIGRATE_URL") or os.environ.get("DATABASE_URL")
    if not url or not db.is_postgres_url(url):
        # SQLite migrates itself on open (store/migrate.py); opening the store is the migration.
        conn = stores.sales()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        print(f"sqlite: schema at version {version} (expected {stores.SCHEMA_VERSION})")
        return 0 if version == stores.SCHEMA_VERSION else 1
    conn = db.connect(url)
    conn.system = True                                     # the migrator acts as nobody, as the owner role
    try:
        current, expected = pgmigrate.check(conn)
        if args.check:
            stale = pgmigrate.stale_repeatables(conn)
            state = "up to date" if current == expected else "BEHIND" if current < expected else "AHEAD of this build"
            print(f"postgres: schema at version {current}, this build expects {expected}: {state}")
            for name in stale:
                print(f"postgres: {name}.sql (row-level security) differs from this build's: run `salescoach migrate`")
            return 0 if current == expected and not stale else 1
        applied = pgmigrate.apply(conn, log=print)
        current, expected = pgmigrate.check(conn)
        stale = pgmigrate.stale_repeatables(conn)
        print(f"postgres: schema at version {current}" + ("" if applied else " (nothing to apply)"))
        stores.forget_verified()
        return 0 if current == expected and not stale else 1
    finally:
        conn.close()


def cmd_import_sqlite(args):
    """A single-user SQLite install into an EMPTY Postgres org as the user with that email
    (salescoach/lifecycle/importer.py; docs/deploy-cloud.md, "Launch checklist")."""
    from .lifecycle import importer, owner
    try:
        report = importer.run(args.path, args.as_email, url=args.url, dry_run=args.dry_run, log=print)
    except (importer.ImportRefused, owner.NoOwnerURL) as exc:
        print(f"import-sqlite: {exc}", file=sys.stderr)
        return 2
    return 0 if (report.committed or report.dry_run) else 1


def cmd_retention(args):
    """Apply (or with --dry-run, count) the org's retention period for every active user, each in their own
    service session: exactly what the scheduler's retention duty does in a round."""
    from . import identity, users
    from .lifecycle import retention, settings
    days = args.days or settings.retention_days()
    if not days:
        print("retention.days is not set (Settings, or config/org.yaml): every call is kept")
        return 0
    with identity.activate(None):
        conn = stores.sales()
    try:
        with conn.as_system():
            targets = [(u["id"], u.get("email") or u["id"]) for u in users.active(conn)]
        total = 0
        for user_id, label in targets:
            with identity.as_user(conn, user_id, mode=identity.SERVICE):
                result = retention.purge(conn, days=days, dry_run=args.dry_run)
            n = result.get("calls", 0)
            total += n
            held = f", {result['held']} held (being processed)" if result.get("held") else ""
            verb = "would delete" if args.dry_run else "deleted"
            print(f"{label}: {verb} {n} call(s) started before {result.get('cutoff')}{held}")
        print(f"{'dry run: ' if args.dry_run else ''}{total} call(s) older than {days} days"
              + (" would be deleted" if args.dry_run else " deleted"))
        if identity.cloud():
            print("disabled users' calls are kept until they are offboarded (Admin: Offboard)")
        return 0
    finally:
        conn.close()


def _worker_listening(port: int = 8140) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def main(argv=None):
    p = argparse.ArgumentParser(prog="salescoach")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8140)
    s.add_argument("--no-worker", action="store_true", help="preview only: no worker, scheduler or Jarvis sync")
    s.add_argument("--allow-unauthenticated", action="store_true",
                   help="serve on a non-loopback --host without SALESCOACH_PASSWORD (a private network you trust)")
    s.add_argument("--role", choices=["all", "web", "worker", "scheduler"], default=os.environ.get("SALESCOACH_ROLE") or "all",
                   help="all (default: one process, as on a laptop), web (HTTP only), worker (WORKER_CONCURRENCY "
                        "loops, no HTTP), scheduler (the duties under a leader election; no HTTP)")
    s.add_argument("--concurrency", type=int, help="worker role: how many loops (default WORKER_CONCURRENCY or 2)")
    s.set_defaults(fn=cmd_serve)

    h = sub.add_parser("health", help="exit 0 when this role's process is healthy (the container health check)")
    h.add_argument("--role", choices=["all", "web", "worker", "scheduler"], default=os.environ.get("SALESCOACH_ROLE") or "all")
    h.add_argument("--port", type=int)
    h.set_defaults(fn=cmd_health)

    pl = sub.add_parser("payloads", help="raw payloads kept in the store (cloud mode)")
    pl.add_argument("action", choices=["export"])
    pl.add_argument("--out", help="folder to write <kind>/<id>.<ext> into (default: JSON lines on stdout)")
    pl.add_argument("--since", help="only payloads created at or after this ISO timestamp")
    pl.add_argument("--json", action="store_true", help="JSON lines on stdout even with --out")
    pl.add_argument("--as", dest="as_user", metavar="USER_ID", help="cloud mode: read as this user (service session)")
    pl.set_defaults(fn=cmd_payloads)

    sub.add_parser("password-hash", help="print a SALESCOACH_PASSWORD_HASH for the password on stdin").set_defaults(
        fn=cmd_password_hash)

    c = sub.add_parser("call")
    c.add_argument("--title", required=True)
    c.add_argument("--deal")
    c.add_argument("--lang", default="auto", choices=["en", "hinglish", "auto"])
    c.add_argument("--participant", action="append")
    c.add_argument("--process", action="store_true")
    c.set_defaults(fn=cmd_call)

    ia = sub.add_parser("import-audio")
    ia.add_argument("path")
    ia.add_argument("--title", required=True)
    ia.add_argument("--deal")
    ia.add_argument("--lang", default="auto", choices=["en", "hinglish", "auto"])
    ia.add_argument("--layout", default="stereo_me_left", choices=["stereo_me_left", "mono_them"])
    ia.add_argument("--participant", action="append")
    ia.set_defaults(fn=cmd_import_audio)

    it = sub.add_parser("import-text")
    it.add_argument("path", help="file, or - for stdin")
    it.add_argument("--title", required=True)
    it.add_argument("--deal")
    it.add_argument("--date", help="ISO timestamp of the call")
    it.add_argument("--lang", default="auto", choices=["en", "hinglish", "auto"])
    it.add_argument("--participant", action="append")
    it.set_defaults(fn=cmd_import_text)

    fi = sub.add_parser("import-file", help="import a transcript exported by any recorder (.txt .vtt .srt .json)")
    fi.add_argument("path")
    fi.add_argument("--title")
    fi.add_argument("--deal")
    fi.add_argument("--me", metavar="LABEL", help='your speaker label in the file, or "none"')
    fi.add_argument("--history", action="store_true", help="old meeting: analyse it, draft no follow-up")
    fi.add_argument("--lang", default="auto", choices=["en", "hinglish", "auto"])
    fi.set_defaults(fn=cmd_import_file)

    pr = sub.add_parser("process")
    pr.add_argument("call_id")
    pr.add_argument("--from", dest="from_step")
    pr.add_argument("--force", action="store_true")
    pr.set_defaults(fn=cmd_process)

    sub.add_parser("work").set_defaults(fn=cmd_work)

    lo = sub.add_parser("loops")
    lo.add_argument("--deal")
    lo.add_argument("--owner", choices=["me", "prospect", "mutual", "internal"])
    lo.add_argument("--status", default="open")
    lo.add_argument("--json", action="store_true")
    lo.set_defaults(fn=cmd_loops)

    d = sub.add_parser("deal")
    d.add_argument("action", choices=["add", "list"])
    d.add_argument("name", nargs="?")
    d.add_argument("--account")
    d.add_argument("--domain", action="append")
    d.add_argument("--stage")
    d.set_defaults(fn=cmd_deal)

    pe = sub.add_parser("person")
    pe.add_argument("action", choices=["add"])
    pe.add_argument("name")
    pe.add_argument("--email")
    pe.add_argument("--title")
    pe.add_argument("--deal")
    pe.add_argument("--role")
    pe.add_argument("--contact-file")
    pe.set_defaults(fn=cmd_person)

    m = sub.add_parser("models")
    m.add_argument("action", choices=["pull", "list", "pull-diarization"])
    m.add_argument("repo", nargs="?")
    m.set_defaults(fn=cmd_models)

    j = sub.add_parser("jarvis")
    j.add_argument("action", choices=["sync", "bootstrap"])
    j.add_argument("--deal")
    j.add_argument("--dry-run", action="store_true")
    j.set_defaults(fn=cmd_jarvis)

    ob = sub.add_parser("onboard", help="create accounts, deals and people from config/accounts.yaml")
    ob.add_argument("--file")
    ob.set_defaults(fn=cmd_onboard)

    bf = sub.add_parser("backfill-granola", help="import past Granola meetings for known deals")
    bf.add_argument("--range", default="last_30_days", choices=["this_week", "last_week", "last_30_days"])
    bf.add_argument("--all", action="store_true", help="also meetings with no known deal")
    bf.add_argument("--dry-run", action="store_true")
    bf.set_defaults(fn=cmd_backfill)

    ev = sub.add_parser("eval")
    ev.add_argument("cases", nargs="*", help="case names in data/eval (default: all)")
    ev.set_defaults(fn=cmd_eval)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    mg = sub.add_parser("migrate", help="apply the Postgres schema migrations (DATABASE_MIGRATE_URL or DATABASE_URL)")
    mg.add_argument("--check", action="store_true", help="exit 1 when the database is behind this build")
    mg.add_argument("--url", help="the postgresql:// URL to migrate (default: the environment)")
    mg.set_defaults(fn=cmd_migrate)

    im = sub.add_parser("import-sqlite", help="import a single-user SQLite install into an empty Postgres org")
    im.add_argument("path", help="the local install's sales.db (never written: a migrated copy is read)")
    im.add_argument("--as", dest="as_email", required=True, metavar="EMAIL",
                    help="the user the data becomes (created as an active rep when missing)")
    im.add_argument("--dry-run", action="store_true", help="do everything, print the counts, roll back")
    im.add_argument("--url", help="the owner role's postgresql:// URL (default: DATABASE_MIGRATE_URL)")
    im.set_defaults(fn=cmd_import_sqlite)

    rt = sub.add_parser("retention", help="apply the org's retention.days now, or --dry-run to count")
    rt.add_argument("--dry-run", action="store_true", help="count what would be deleted; delete nothing")
    rt.add_argument("--days", type=int, help="override retention.days for this run")
    rt.set_defaults(fn=cmd_retention)

    from . import plugins
    plugins.register_cli(sub)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
