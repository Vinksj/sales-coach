"""Phase 4 plugin: agentic execution (salescoach/automation).

What it attaches to the core, through the plugin seam only:
  NAV                the Follow-ups page
  router             /followups, /nudges/<id>, /replies, /emails/<id>/fill-slots,
                     /automation/today. Built on first access (module
                     __getattr__), so the CLI and the worker never pay for
                     importing the web layer.
  register           EMAIL_SENT -> nudge accounting (follow_up_count moves only
                     on a real send); STAKEHOLDER_REPLY_RECEIVED -> reply
                     analysis; NEXT_CALL_SCHEDULED -> prep brief; FOLLOW_UP_RUN
                     and FOLLOW_UP_NUDGE -> the follow-up agent, in the worker.
  register_cli       followups, replies, calendar, autosend
  start_background   the scheduler threads (automation/scheduler.py)
Tables: execution.sql. Config: config/automation.yaml, config/scheduling.yaml.
"""
import json
from datetime import date

NAV = [("/calendar", "Calendar"), ("/followups", "Follow-ups")]


def __getattr__(name):
    if name == "router":
        from ..automation.web import router
        return router
    raise AttributeError(name)


# ---- workflow -------------------------------------------------------------------------

def _run_followups(conn, event):
    from ..automation import followup
    day = (event.payload or {}).get("date")
    followup.evaluate_due(conn, date.fromisoformat(day) if day else None)


def _nudge_now(conn, event):
    from ..automation import followup
    followup.evaluate_one(conn, (event.payload or {}).get("loop_id") or event.entity_id, force_nudge=True)


def register(workflow):
    from ..automation import calendar, followup, replies
    workflow.register_handler("EMAIL_SENT", followup.on_email_sent)
    workflow.register_handler("STAKEHOLDER_REPLY_RECEIVED", replies.on_reply)
    workflow.register_handler("NEXT_CALL_SCHEDULED", calendar.on_next_call)
    workflow.register_handler("CALENDAR_REFRESH_REQUESTED", calendar.on_refresh)
    workflow.register_handler("FOLLOW_UP_RUN", _run_followups)
    workflow.register_handler("FOLLOW_UP_NUDGE", _nudge_now)


def start_background(db_path, stop):
    from ..automation import scheduler
    scheduler.start(db_path, stop)


# ---- CLI --------------------------------------------------------------------------------

def _conn():
    from ..store import stores
    return stores.sales()


def _cli_followups(args):
    from .. import seller
    from ..automation import common, followup
    conn = _conn()
    if args.why:
        print(followup.explain(followup.why(conn, args.why)))
        return
    today = date.fromisoformat(args.date) if args.date else common.today_ist()
    if args.run:
        for r in followup.evaluate_due(conn, today):
            extra = f" -> email {r['email_id']}" if r.get("email_id") else ""
            print(f"{r['loop_id']}  [{seller.display(r['stage'])}/{r['check']}] {seller.display(r['decision'])}{extra}\n"
                  f"    {r['rationale']}")
        return
    for loop in followup.due_loops(conn, today):
        print(f"{loop['node_id']}  check {loop['next_check_at']}  {loop['owner']:<8} nudges {loop['follow_up_count']}  "
              f"[{loop['deal_name'] or '-'}] {loop['description']}")
    print("(add --run to decide them)")


def _cli_replies(args):
    from ..automation import replies
    from ..automation.scheduler import _gmail
    print(json.dumps(replies.poll(_conn(), _gmail()), indent=1))
    print("New replies are analysed by the workflow worker (salescoach serve, or salescoach work).")


def _cli_calendar(args):
    from .. import seller
    from ..automation import calendar, common, connector
    conn = _conn()
    if args.action == "tools":
        print(json.dumps(connector.discover(conn, refresh=args.refresh), indent=1))
        return
    if args.action == "upcoming":
        print(json.dumps(calendar.upcoming_deal_meetings(conn, days=args.days or None), indent=1))
        return
    cfg = calendar.scheduling({"horizon_business_days": args.days} if args.days else None)
    current = common.now_ist()
    start, end = calendar.search_window(current, cfg)
    events = calendar.ConnectorCalendar(conn).events(start, end)
    busy = calendar.busy_intervals(events)
    print(f"Window {start:%a %d %b} to {end:%a %d %b}: {len(events)} events, {len(busy)} busy")
    for s, e in busy:
        print(f"  busy {s.astimezone(common.IST):%a %d %b %H:%M}-{e.astimezone(common.IST):%H:%M}")
    slots = calendar.compute_slots(busy, current, cfg)
    for s in slots:
        print(f"  slot {calendar.slot_label(s)} {seller.tz_label(s)}")
    print(calendar.slots_sentence(slots) if len(slots) >= 2 else "Fewer than two free slots.")


def _cli_autosend(args):
    from ..automation import autosend
    print(json.dumps(autosend.status(_conn()), indent=1, default=str))


def register_cli(sub):
    f = sub.add_parser("followups", help="follow-up agent: due loops, decisions, why a nudge was sent")
    f.add_argument("--run", action="store_true", help="decide every due loop now")
    f.add_argument("--date", help="evaluate as of this date in your timezone (YYYY-MM-DD)")
    f.add_argument("--why", type=int, metavar="EMAIL_ID", help="explain one nudge")
    f.set_defaults(fn=_cli_followups)
    r = sub.add_parser("replies", help="poll Gmail threads of sent emails for buyer replies")
    r.add_argument("action", choices=["poll"])
    r.set_defaults(fn=_cli_replies)
    c = sub.add_parser("calendar", help="read-only calendar: tool names, proposed slots, deal meetings")
    c.add_argument("action", choices=["tools", "slots", "upcoming"])
    c.add_argument("--days", type=int, help="business days for slots, calendar days for upcoming")
    c.add_argument("--refresh", action="store_true", help="re-discover the connector's tool names")
    c.set_defaults(fn=_cli_calendar)
    a = sub.add_parser("autosend", help="auto-send executor status")
    a.add_argument("action", choices=["status"])
    a.set_defaults(fn=_cli_autosend)
