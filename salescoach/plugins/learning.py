"""Phase F plugin: the learning layer (salescoach/learning).

What it attaches, through the plugin seam only:
  NAV               the Learning page ("what the coach believes")
  router            /learning, /learning/*, /deals/<id>/outcome. Built on first access,
                    so the CLI and the worker never import the web layer.
  register          LEARNING_RECOMPUTE, and the same recompute after
                    POST_CALL_ANALYSIS_COMPLETE, EMAIL_SENT and STAKEHOLDER_REPLY_RECEIVED.
                    It is idempotent and never raises into the pipeline: a failure is
                    rolled back, logged and recorded in state learning:last_error.
  register_cli      salescoach learn --recompute | --show | --digest [--days N]
  start_background  one daily recompute (windows close with time, not only with events)
  digest            digest(conn, since): what changed, as a dict with a plain-text "text". It is
                    shown on the Learning page and printed by the CLI; it is never emailed or scheduled.
Tables: learning.sql. Thresholds: config/learning.yaml. No model is called here. What is learned
reaches the prompts through salescoach/learning/feedback.py (phase F2), one flag per target.
"""
import json
import logging

from .. import learning as _learning  # noqa: F401  (registers the gated fields)

log = logging.getLogger("salescoach.learning")
NAV = [("/learning", "Learning")]
DAILY_S = 24 * 3600


def __getattr__(name):
    if name == "router":
        from ..learning.web import router
        return router
    raise AttributeError(name)


def digest(conn, since) -> dict:
    from ..learning import weekly
    return weekly.digest(conn, since)


# ---- workflow -----------------------------------------------------------------------------

def run_recompute(conn, trigger: str = "manual") -> dict:
    """outcomes.recompute then patterns.recompute, committed together. Never raises."""
    from .. import identity
    from ..learning import outcomes, patterns
    from ..store.stores import now, set_user_state as set_state
    try:
        if conn.in_transaction:
            conn.commit()
        # The acting user's own learning, whoever asked (a manager's button on their own Learning page too).
        # A SERVICE binding makes the database agree: on Postgres a service session reads only the actor's own
        # rows, so a collector that forgot its owner filter still cannot see (and copy) a team member's rows.
        actor = identity.actor_of(conn)
        with identity.as_actor(conn, actor.as_service()):
            result = {"trigger": trigger, "at": now(), "outcomes": outcomes.recompute(conn),
                      "patterns": patterns.recompute(conn)}
        set_state(conn, "learning:last_run", json.dumps(result, default=str)[:4000])
        set_state(conn, "learning:last_error", "")
        conn.commit()
        return result
    except Exception as exc:
        log.exception("learning recompute (%s) failed", trigger)
        error = f"{type(exc).__name__}: {exc}"[:1000]
        try:
            conn.rollback()
            set_state(conn, "learning:last_error", json.dumps({"at": now(), "trigger": trigger, "error": error}))
            conn.commit()
        except Exception:
            log.exception("could not record the learning failure")
        return {"trigger": trigger, "error": error}


def _on(trigger):
    def handler(conn, event):
        run_recompute(conn, trigger=trigger)
    handler.__name__ = f"learning_after_{trigger}"
    return handler


_HANDLERS = {"LEARNING_RECOMPUTE": _on("requested"), "POST_CALL_ANALYSIS_COMPLETE": _on("analysis"),
             "EMAIL_SENT": _on("email_sent"), "STAKEHOLDER_REPLY_RECEIVED": _on("reply")}


def register(workflow):
    for event_type, handler in _HANDLERS.items():
        workflow.register_handler(event_type, handler)


# ---- serve -------------------------------------------------------------------------------------

def start_background(db_path, stop):
    from ..automation.scheduler import Duty, start
    start(db_path, stop, duties=[Duty("learning", lambda conn: run_recompute(conn, trigger="daily"),
                                      lambda: DAILY_S, first_delay_s=300)])


# ---- CLI -------------------------------------------------------------------------------------------

def _show(conn) -> str:
    from ..learning import outcomes, patterns
    lines = []
    for fam in patterns.beliefs(conn):
        if not fam["patterns"]:
            continue
        lines.append(f"\n{fam['title']}")
        for p in fam["patterns"]:
            tags = " ".join(t for t in (p["label"], "returned" if p["returned"] else "",
                                        f"you:{p['user_state']}" if p["user_state"] else "") if t)
            lines.append(f"  [{p['status']:<9}] {p['summary']}  (calls {p['n_calls']}, deals {p['n_deals']}, "
                         f"obs {p['n_obs']}){'  ' + tags if tags else ''}\n              {p['id']}")
    for s in patterns.series(conn):
        lines.append(f"\nseries {s['key']} (n={s['n']}): " + ", ".join(f"{pt['value']:g}" for pt in s["points"][-10:]))
    proposals = patterns.open_proposals(conn)
    if proposals:
        lines.append("\nOpen proposals (accept or dismiss on /learning)")
        lines += [f"  #{p['id']} {p['summary']}" for p in proposals]
    lines.append("\nDerived outcomes")
    lines += [f"  {c['kind']:<22} yes {c['yes']}  no {c['no']}  open {c['pending']}" for c in outcomes.counts(conn)]
    return "\n".join(lines).strip() or "Nothing learned yet."


def cmd_learn(args):
    from ..learning import ensure_columns
    from ..store import stores
    conn = stores.sales()
    ensure_columns(conn)
    if args.recompute:
        result = run_recompute(conn, trigger="cli")
        print(json.dumps(result, indent=1, default=str))
        if result.get("error"):
            return 1
    wants_digest = getattr(args, "digest", False)       # a caller built before --digest existed has no such field
    if wants_digest:
        from ..learning import weekly
        print(weekly.last_days(conn, getattr(args, "days", 7) or 7)["text"])
    if args.show or not (args.recompute or wants_digest):
        print(_show(conn))


def register_cli(sub):
    p = sub.add_parser("learn", help="learning layer: recompute outcomes and patterns, or show what the coach believes")
    p.add_argument("--recompute", action="store_true", help="rebuild derived outcomes and learned patterns now")
    p.add_argument("--show", action="store_true", help="print patterns, open proposals and outcome counts")
    p.add_argument("--digest", action="store_true", help="print what changed lately (nothing is sent anywhere)")
    p.add_argument("--days", type=int, default=7, help="how far back --digest looks (default 7)")
    p.set_defaults(fn=cmd_learn)
