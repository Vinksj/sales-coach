"""Phase 3 plugin: sales intelligence.

Attaches to the core without editing it:
  * pipeline step 'strategized' after loops_reconciled: the Deal Strategist,
    reconciliation of analyst vs strategist, all through the memory gate;
  * handlers: the longitudinal coach report refreshes after each analysed
    call and after each review; on-demand strategy, prep and coach requests
    from the UI are queued events the worker runs;
  * routes and fragments (intel/web.py), CLI commands, and a background
    thread in `salescoach serve` that keeps the embedding index fresh;
  * schema: plugins/intelligence.sql.

Importing intel.tables registers the gated tables, so conflict resolution in
the core UI works for stakeholder and MEDDPICC disagreements too.
"""
import json
import logging
import threading

from ..intel import tables  # noqa: F401  (registers the gated tables)
from ..intel.web import router  # noqa: F401

log = logging.getLogger("salescoach.intel")
EMBED_EVERY_S = 30 * 60


# ---- workflow -------------------------------------------------------------------

def _coach(trigger):
    def handler(conn, event):
        from ..intel import coach
        try:
            coach.refresh(conn, trigger=trigger)
        except Exception:                        # a coach report never fails the event that triggered it
            conn.rollback()
            log.exception("coach refresh after %s failed", event.type)
    handler.__name__ = f"coach_after_{trigger}"
    return handler


_on_analysis = _coach("analysis")
_on_review = _coach("review")


def _on_strategy(conn, event):
    from ..intel import strategist
    strategist.run_for_deal(conn, event.entity_id, force=bool(event.payload.get("force")))


def _on_prep(conn, event):
    from ..intel import prep
    p = event.payload
    prep.generate(conn, event.entity_id, meeting_title=p.get("title"), attendees=p.get("attendees") or (),
                  when=p.get("when"))


def _on_coach_request(conn, event):
    from ..intel import coach
    coach.refresh(conn, trigger="manual", force=True)


def register(workflow):
    from ..intel import strategist
    workflow.register_step("strategized", strategist.step, after="loops_reconciled", label="Deal strategy")
    workflow.register_handler("POST_CALL_ANALYSIS_COMPLETE", _on_analysis)
    workflow.register_handler("REVIEW_COMPLETED", _on_review)
    workflow.register_handler("STRATEGY_REQUESTED", _on_strategy)
    workflow.register_handler("PREP_REQUESTED", _on_prep)
    workflow.register_handler("COACH_REPORT_REQUESTED", _on_coach_request)


# ---- CLI ----------------------------------------------------------------------------

def _conn():
    from ..store import stores
    return stores.sales()


def _rate_limited(exc) -> int:
    print(f"the model quota is exhausted; nothing was changed. Try again later. ({exc})")
    return 1


def cmd_strategy(args):
    from ..agents.base import AgentFailed
    from ..intel import strategist
    conn = _conn()
    try:
        result = strategist.run_for_deal(conn, args.deal, force=args.force)
    except AgentFailed as exc:
        return _rate_limited(exc)
    except LookupError as exc:
        print(exc)
        return 1
    if result is None:
        failed, _ = tables.latest_strategy(conn, args.deal, include_failed=True)
        print(f"strategy failed: {(failed or {}).get('failed', 'unknown error')}")
        return 1
    print(json.dumps(result, indent=1, ensure_ascii=False) if args.json else strategist.render_text(conn, args.deal))


def cmd_prep(args):
    from ..intel import prep
    conn = _conn()
    brief_id = prep.generate(conn, args.deal, meeting_title=args.title, attendees=args.attendee or (),
                             when=args.when, use_llm=not args.no_llm)
    brief = prep.get(conn, brief_id=brief_id)
    print(json.dumps(brief, indent=1, ensure_ascii=False) if args.json else prep.render_text(brief))


def cmd_coach_report(args):
    from ..agents.base import AgentFailed
    from ..intel import coach
    conn = _conn()
    try:
        rid = coach.refresh(conn, trigger="manual", force=args.force)
    except AgentFailed as exc:
        return _rate_limited(exc)
    report = coach.latest(conn)
    if report is None:
        print("no coach report: no analysed calls yet, or the coach agent failed (see state intel:coach_error)")
        return 1
    if rid is None and not args.force:
        print("(inputs unchanged; showing the latest report)")
    print(json.dumps(report, indent=1, ensure_ascii=False))


def cmd_embed_index(args):
    from ..intel import embed
    conn = _conn()
    print(json.dumps({**embed.index_pending(conn), "stored": embed.stats(conn)}, indent=1))


def register_cli(sub):
    s = sub.add_parser("strategy", help="run the Deal Strategist for a deal")
    s.add_argument("--deal", required=True)
    s.add_argument("--force", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_strategy)
    p = sub.add_parser("prep", help="prepare a pre-call brief for a deal")
    p.add_argument("--deal", required=True)
    p.add_argument("--title")
    p.add_argument("--attendee", action="append", help="email or name; repeatable")
    p.add_argument("--when", help="ISO date/time of the meeting")
    p.add_argument("--no-llm", action="store_true", help="skip the prep writer; template opening and close")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_prep)
    c = sub.add_parser("coach-report", help="regenerate the longitudinal coach report")
    c.add_argument("--force", action="store_true")
    c.set_defaults(fn=cmd_coach_report)
    sub.add_parser("embed-index", help="index claims, insights, observations, transcript windows").set_defaults(
        fn=cmd_embed_index)


# ---- serve ----------------------------------------------------------------------------

def start_background(db_path, stop):
    def loop():
        from .. import identity, users
        from ..intel import embed
        from ..store import stores
        delay = 60
        while stop is None or not stop.wait(delay):
            try:
                with identity.activate(None):
                    conn = stores.sales(db_path)
                try:
                    for user in users.active(conn):            # embeddings are OWNED: indexed as their owner
                        with identity.as_user(conn, user["id"], mode=identity.SERVICE):
                            embed.index_pending(conn)
                finally:
                    conn.close()
            except Exception:
                log.exception("embedding index refresh failed")
            delay = EMBED_EVERY_S
            if stop is None:
                return
    threading.Thread(target=loop, name="salescoach-intel-embed", daemon=True).start()
