"""Phase 4 pages: Follow-ups, one nudge's review page, Replies, and the
calendar fill for any draft's [SLOTS].

Built on the core web layer (render, the per-request connection, the redirect
and form helpers), so the same-origin guard and page chrome apply unchanged.
Nudges have their own Send and Save-to-Drafts handlers because a nudge belongs
to a loop, not a call: the core email handlers close the call's review after
sending, which is wrong for a nudge. Both still go through
policy.approve_and_send only.

Work that needs a model (evaluating loops, drafting a nudge, analysing a
reply) is queued as a workflow event for the worker, never run inside a page
request. Today's sections come from today_data(), registered as a template
global so the core Today page can include them without a core change.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import identity
from ..execution import policy, tokens
from ..orchestrator import bus, review
from ..schemas.events import Event
from ..store import stores
from ..store.stores import get_user_state, now
from ..validators import recipients as recipients_v
from ..web import app as core
from . import autosend, calendar, common, followup

router = APIRouter()


# ---- data shared with the core Today page ----------------------------------------------

def _latest_decisions(conn, loop_ids) -> dict:
    if not loop_ids:
        return {}
    rows = conn.execute(
        f"SELECT * FROM followup_decisions WHERE loop_id IN ({','.join('?' * len(loop_ids))}) ORDER BY id",
        list(loop_ids)).fetchall()
    return {r["loop_id"]: dict(r) for r in rows}


def _drafted_nudges(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT e.*, d.name AS deal_name FROM emails e LEFT JOIN deals d ON d.node_id=e.deal_id "
            "WHERE e.kind='nudge' AND e.status IN ('drafted','failed','saved_to_gmail','sending') ORDER BY e.id DESC"):
        n = dict(r)
        n["to"] = core.fromjson(n["to_addrs"], []) or []
        dec = conn.execute("SELECT f.*, l.description FROM followup_decisions f JOIN loops l ON l.node_id=f.loop_id "
                           "WHERE f.email_id=? ORDER BY f.id DESC LIMIT 1", (n["id"],)).fetchone()
        n["decision"] = dict(dec) if dec else None
        out.append(n)
    return out


def _upcoming(conn, limit=8) -> list[dict]:
    return calendar.upcoming_meetings(conn, limit=limit)


def _calendar_state(conn, request) -> dict:
    return {"last_sync": core.fromjson(get_user_state(conn, calendar.LAST_SYNC_KEY), None),
            "unavailable": core.fromjson(get_user_state(conn, "automation:calendar:unavailable"), None),
            "refresh_pending": calendar.refresh_pending(conn), "live": core._live_status(request.app),
            "record_cfg": common.cfg("calendar").get("record") or {}}


def _recent_replies(conn, limit=10, include_reviewed=False) -> list[dict]:
    where = "" if include_reviewed else "WHERE r.status!='reviewed'"
    out = []
    for r in conn.execute(
            f"SELECT r.*, d.name AS deal_name, e.subject AS our_subject, e.kind AS our_kind FROM email_replies r "
            f"LEFT JOIN deals d ON d.node_id=r.deal_id LEFT JOIN emails e ON e.id=r.email_id "
            f"WHERE r.owner_id=? {where.replace('WHERE', 'AND')} ORDER BY r.received_at DESC LIMIT ?",
            (identity.actor_of(conn).user_id, limit)):
        rep = dict(r)
        rep["ignored"] = core.fromjson(rep["ignored_instructions"], []) or []
        props = []
        for p in conn.execute(
                "SELECT p.*, l.description AS loop_description, l.review_state AS loop_review, l.status AS loop_status, "
                "mc.status AS conflict_status, nl.review_state AS new_review, nl.description AS new_description "
                "FROM reply_proposals p LEFT JOIN loops l ON l.node_id=p.loop_id "
                "LEFT JOIN memory_conflicts mc ON mc.id=p.conflict_id LEFT JOIN loops nl ON nl.node_id=p.new_loop_id "
                "WHERE p.reply_id=? ORDER BY p.id", (rep["id"],)):
            prop = dict(p)
            prop["notes_list"] = core.fromjson(prop["notes"], []) or []
            props.append(prop)
        rep["proposals"] = props
        rep["open_items"] = sum(1 for p in props if p["conflict_status"] == "open" or p["new_review"] == "proposed")
        out.append(rep)
    return out


def today_data(request) -> dict:
    """Follow-ups due, replies received and upcoming calls, for the Today page."""
    try:
        conn = stores.sales(request.app.state.db_path)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        today = common.today_ist()
        due = [dict(r) for r in followup.due_loops(conn, today)]
        decided = conn.execute(
            "SELECT f.*, l.description, d.name AS deal_name FROM followup_decisions f JOIN loops l ON l.node_id=f.loop_id "
            "LEFT JOIN deals d ON d.node_id=f.deal_id WHERE f.eval_date=? AND f.stage IN ('rules','agent') "
            "AND f.decision IN ('ask_user','escalate','close_as_stale') ORDER BY f.id DESC LIMIT 10",
            (today.isoformat(),)).fetchall()
        return {"due": due, "decided": [dict(r) for r in decided], "nudges": _drafted_nudges(conn),
                "replies": _recent_replies(conn, 6), "upcoming": _upcoming(conn),
                "calendar": _calendar_state(conn, request),
                "last_eval": core.fromjson(get_user_state(conn, "automation:followups:last_eval"), None)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def slot_fill_status(request, email_id) -> dict | None:
    try:
        conn = stores.sales(request.app.state.db_path)
    except Exception:
        return None
    try:
        row = calendar.last_fill(conn, email_id)
        return dict(row) if row else None
    finally:
        conn.close()


core.templates.env.globals["automation_today"] = today_data
core.templates.env.globals["automation_slot_fill"] = slot_fill_status


# ---- follow-ups ---------------------------------------------------------------------------

# ---- calendar: every meeting, and which ones to record -----------------------------------

@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    with core._db(request) as conn:
        meetings = calendar.upcoming_meetings(conn)
        state = _calendar_state(conn, request)
        days = int(common.cfg("calendar").get("upcoming_days", 7))
        return core.render(request, conn, "automation_calendar.html", meetings=meetings, days=days, **state,
                           autorefresh=15 if state["refresh_pending"] else 0)


@router.post("/calendar/refresh")
def calendar_refresh(request: Request, next_url: str = Form("/calendar", alias="next")):
    back = core._clean_next(next_url, "/calendar")
    with core._db(request) as conn:
        calendar.request_refresh(conn)
    return core._redirect(back, msg="Reading your calendar through the connector; this takes about a minute.",
                          anchor="upcoming-calls")


@router.post("/calendar/{event_id}/record")
def calendar_record(request: Request, event_id: str, action: str = Form("arm"),
                    next_url: str = Form("/calendar", alias="next")):
    back = core._clean_next(next_url, "/calendar")
    with core._db(request) as conn:
        row = conn.execute("SELECT title FROM calendar_meetings WHERE owner_id=? AND event_id=?",
                           (identity.actor_of(conn).user_id, event_id)).fetchone()
        if row is None:
            return core._redirect(back, err="That meeting is no longer on the calendar.", anchor="upcoming-calls")
        title = row["title"] or "Untitled"
        if action == "arm":
            calendar.set_record(conn, event_id, True)
            return core._redirect(back, msg=f"Will record \"{title}\" when it starts.", anchor="upcoming-calls")
        if action == "disarm":
            calendar.set_record(conn, event_id, False)
            return core._redirect(back, msg=f"Will not record \"{title}\".", anchor="upcoming-calls")
        if action == "now":
            try:
                call_id = calendar.start_recording(conn, event_id, core._live_manager(request.app))
            except calendar.RecordingRefused as exc:
                return core._redirect(back, err=str(exc), anchor="upcoming-calls")
            except Exception as exc:
                live = core._live_status(request.app)
                if type(exc).__name__ == "LiveCallActive" and live.get("call_id"):
                    return core._redirect(f"/live/{live['call_id']}",
                                          err="A call is already live. Stop it before starting another.")
                return core._redirect(back, err=f"Could not start capture: {type(exc).__name__}: {exc}",
                                      anchor="upcoming-calls")
            return core._redirect(f"/live/{call_id}")
    return core._redirect(back, err="Unknown action.", anchor="upcoming-calls")


@router.get("/followups", response_class=HTMLResponse)
def followups_page(request: Request):
    today = common.today_ist()
    with core._db(request) as conn:
        due = [dict(r) for r in followup.due_loops(conn, today)]
        latest = _latest_decisions(conn, [l["node_id"] for l in due])
        for loop in due:
            loop["last"] = latest.get(loop["node_id"])
        recent = [dict(r) for r in conn.execute(
            "SELECT f.*, l.description, l.owner, l.owner_name, l.follow_up_count, d.name AS deal_name, "
            "e.status AS email_status FROM followup_decisions f JOIN loops l ON l.node_id=f.loop_id "
            "LEFT JOIN deals d ON d.node_id=f.deal_id LEFT JOIN emails e ON e.id=f.email_id "
            "WHERE f.eval_date>=? ORDER BY f.id DESC LIMIT 80", ((today - timedelta(days=14)).isoformat(),))]
        proposals = [dict(r) for r in conn.execute(
            "SELECT mc.*, l.description FROM memory_conflicts mc JOIN loops l ON l.node_id=mc.entity_id "
            "WHERE mc.status='open' AND mc.provenance LIKE '%\"kind\": \"followup\"%' ORDER BY mc.id DESC")]
        for p in proposals:
            p["reason"] = (core.fromjson(p["provenance"], {}) or {}).get("reason")
        escalations = [dict(r) for r in conn.execute(
            "SELECT l.*, d.name AS deal_name FROM loops l LEFT JOIN deals d ON d.node_id=l.deal_id "
            "WHERE l.node_id LIKE 'loop-esc-%' AND l.status IN ('open','waiting') AND l.review_state!='rejected' "
            "ORDER BY l.created_at DESC")]
        sent = [dict(r) for r in conn.execute(
            "SELECT e.*, d.name AS deal_name FROM emails e LEFT JOIN deals d ON d.node_id=e.deal_id "
            "WHERE e.kind='nudge' AND e.status='sent' ORDER BY e.sent_at DESC LIMIT 10")]
        for s in sent:
            s["to"] = core.fromjson(s["to_addrs"], []) or []
        return core.render(request, conn, "automation_followups.html", due=due, recent=recent,
                           drafted=_drafted_nudges(conn), proposals=proposals, escalations=escalations, sent=sent,
                           last_eval=core.fromjson(get_user_state(conn, "automation:followups:last_eval"), None),
                           run_at=common.cfg("followup").get("run_at", "09:30"), today_label=core.fmt_day(today),
                           autosend=autosend.status(conn))


@router.post("/followups/run")
def followups_run(request: Request):
    with core._db(request) as conn:
        owner = identity.actor_of(conn).user_id           # whose follow-ups: the worker runs it as this user
        bus.publish(conn, Event(type="FOLLOW_UP_RUN", entity_id=f"user:{owner}",
                                dedupe_key=f"FOLLOW_UP_RUN:{owner}:{now()}", payload={}))
        conn.commit()
    return core._redirect("/followups", msg="Queued. Decisions appear here as the worker gets to them.")


@router.post("/followups/{loop_id}/nudge")
def followups_nudge(request: Request, loop_id: str, next_url: str = Form("", alias="next")):
    with core._db(request) as conn:
        core._loop_or_404(conn, loop_id)
        bus.publish(conn, Event(type="FOLLOW_UP_NUDGE", entity_id=loop_id,
                                dedupe_key=f"FOLLOW_UP_NUDGE:{identity.actor_of(conn).user_id}:{loop_id}:{now()}",
                                payload={"loop_id": loop_id}))
        conn.commit()
    return core._redirect(core._clean_next(next_url, "/followups"),
                          msg="Drafting a nudge. It appears under Nudges to send; nothing is sent until you press Send.")


@router.post("/followups/{loop_id}/snooze")
def followups_snooze(request: Request, loop_id: str, until: str = Form(""), next_url: str = Form("", alias="next")):
    back = core._clean_next(next_url, "/followups")
    try:
        day = date.fromisoformat(until.strip())
    except ValueError:
        return core._redirect(back, err="Pick a date to look again.")
    with core._db(request) as conn:
        core._loop_or_404(conn, loop_id)
        followup.snooze(conn, loop_id, day)
    return core._redirect(back, msg=f"Snoozed until {core.fmt_day(day.isoformat())}.")


@router.post("/followups/{loop_id}/still-open")
def followups_still_open(request: Request, loop_id: str, next_url: str = Form("", alias="next")):
    with core._db(request) as conn:
        core._loop_or_404(conn, loop_id)
        followup.still_open(conn, loop_id)
    return core._redirect(core._clean_next(next_url, "/followups"),
                          msg="Noted as still open. The next check will look at it afresh.")


# ---- one nudge --------------------------------------------------------------------------

@router.get("/nudges/{email_id}", response_class=HTMLResponse)
def nudge_page(request: Request, email_id: int):
    with core._db(request) as conn:
        email = core._email_or_404(conn, email_id)
        info = followup.why(conn, email_id)
        for d in info["history"]:
            d["facts_d"] = core.fromjson(d["facts"], {}) or {}
        for a in info["autosend"]:
            a["reasons_list"] = core.fromjson(a["reasons"], []) or []
        verdict = autosend.check(conn, email) if email["status"] == "drafted" else None
        fill = calendar.last_fill(conn, email_id)
        return core.render(request, conn, "automation_nudge.html", email=core._email_view(conn, email), info=info,
                           verdict=verdict, fill=dict(fill) if fill else None, here=f"/nudges/{email_id}",
                           deal=conn.execute("SELECT * FROM deals WHERE node_id=?", (email["deal_id"],)).fetchone()
                           if email["deal_id"] else None)


@router.post("/nudges/{email_id}/save")
def nudge_save(request: Request, email_id: int, to: str = Form(""), cc: str = Form(""), subject: str = Form(""),
               body: str = Form("")):
    with core._db(request) as conn:
        row = core._email_or_404(conn, email_id)
        if row["status"] not in ("drafted", "failed"):
            return core._redirect(f"/nudges/{email_id}", err="Only a draft can be edited.")
        try:
            core._save_form_edits(conn, row, to, cc, subject, body)
        except ValueError as exc:
            return core._redirect(f"/nudges/{email_id}", err=str(exc))
    return core._redirect(f"/nudges/{email_id}", msg="Draft saved.")


def _approve_nudge(request: Request, email_id: int, mode: str, to, cc, subject, body):
    back = f"/nudges/{email_id}"
    what = "Send" if mode == "send" else "Save to Gmail Drafts"
    with core._db(request) as conn:
        row = core._email_or_404(conn, email_id)
        if row["kind"] != "nudge":
            return core._redirect(back, err="This page only sends nudges; use the call page for follow-ups.")
        try:
            row = core._save_form_edits(conn, row, to, cc, subject, body)
        except ValueError as exc:
            return core._redirect(back, err=str(exc))
        allowed = recipients_v.allowed_recipients(conn, row["call_id"], row["deal_id"])
        addresses = (core.fromjson(row["to_addrs"], []) or []) + (core.fromjson(row["cc_addrs"], []) or [])
        user_added = [a for a in addresses if a.lower() not in allowed]
        try:
            result = policy.approve_and_send(conn, email_id, core.gmail_for(request, conn), mode=mode,
                                             user_added=user_added)
        except policy.SendRefused as exc:
            return core._redirect(back, err=f"{what} refused: {exc}")
        except tokens.TokenError as exc:
            return core._redirect(back, err=f"{what} refused: {exc if isinstance(exc, tokens.NeedsReconsent) else core.NOT_CONNECTED}")
        except Exception as exc:
            return core._redirect(back, err=f"{what} failed: {type(exc).__name__}: {exc}")
    if result.get("duplicate"):
        return core._redirect(back, msg="Already done. Nothing was sent again.")
    return core._redirect(back, msg="Sent." if mode == "send" else "Saved to Gmail Drafts. Send it from Gmail.")


@router.post("/nudges/{email_id}/send")
def nudge_send(request: Request, email_id: int, to: str = Form(None), cc: str = Form(None),
               subject: str = Form(None), body: str = Form(None)):
    return _approve_nudge(request, email_id, "send", to, cc, subject, body)


@router.post("/nudges/{email_id}/draft")
def nudge_draft(request: Request, email_id: int, to: str = Form(None), cc: str = Form(None),
                subject: str = Form(None), body: str = Form(None)):
    return _approve_nudge(request, email_id, "draft", to, cc, subject, body)


@router.post("/nudges/{email_id}/discard")
def nudge_discard(request: Request, email_id: int):
    with core._db(request) as conn:
        core._email_or_404(conn, email_id)
        review.skip_email(conn, email_id, reason="discarded in follow-ups")
        conn.commit()
    return core._redirect("/followups", msg="Nudge discarded. The loop is looked at again on its next check.")


@router.post("/nudges/{email_id}/mark-sent")
def nudge_mark_sent(request: Request, email_id: int):
    """The seller sent the Gmail draft by hand: record it so the loop's follow-up count moves."""
    back = f"/nudges/{email_id}"
    with core._db(request) as conn:
        core._email_or_404(conn, email_id)
        try:
            marked = policy.mark_sent_manually(conn, email_id)
        except policy.SendRefused as exc:
            return core._redirect(back, err=f"Refused: {exc}")
        if not marked:
            return core._redirect(back, err="Only a nudge saved to Gmail Drafts can be marked sent.")
    return core._redirect(back, msg="Marked as sent from Gmail.")


@router.post("/nudges/{email_id}/not-sent")
def nudge_not_sent(request: Request, email_id: int):
    """The seller checked Gmail Sent after an unknown-delivery error and the nudge is not there.
    The core /emails/<id>/not-sent returns to a call page, and a nudge has no call."""
    back = f"/nudges/{email_id}"
    with core._db(request) as conn:
        core._email_or_404(conn, email_id)
        try:
            released = policy.acknowledge_not_sent(conn, email_id)
        except policy.SendRefused as exc:
            return core._redirect(back, err=f"Refused: {exc}")
        if not released:
            return core._redirect(back, err="Only an email stuck in sending can be released.")
    return core._redirect(back, msg="Marked as not sent. You can send it again.")


@router.post("/emails/{email_id}/fill-slots")
def email_fill_slots(request: Request, email_id: int, to: str = Form(None), cc: str = Form(None),
                     subject: str = Form(None), body: str = Form(None), next_url: str = Form("", alias="next")):
    with core._db(request) as conn:
        row = core._email_or_404(conn, email_id)
        default = f"/nudges/{email_id}" if row["kind"] == "nudge" or not row["call_id"] else f"/calls/{row['call_id']}#email"
        back = core._clean_next(next_url, default)
        try:
            core._save_form_edits(conn, row, to, cc, subject, body)
        except ValueError as exc:
            return core._redirect(back, err=str(exc))
        factory = getattr(request.app.state, "calendar_factory", None)
        result = calendar.fill_slots(conn, email_id, calendar=factory(conn) if factory else None)
    if result["status"] == "filled":
        return core._redirect(back, msg=f"Calendar-verified times filled in: {result['sentence']}")
    return core._redirect(back, err=f"Times not filled: {result['reason']}")


# ---- replies ---------------------------------------------------------------------------

@router.get("/replies", response_class=HTMLResponse)
def replies_page(request: Request):
    show_all = request.query_params.get("all") == "1"
    with core._db(request) as conn:
        return core.render(request, conn, "automation_replies.html",
                           replies=_recent_replies(conn, 60, include_reviewed=show_all), show_all=show_all,
                           last_poll=core.fromjson(get_user_state(conn, "automation:replies:last_poll"), None))


@router.post("/replies/poll")
def replies_poll(request: Request):
    from . import replies
    with core._db(request) as conn:
        try:
            result = replies.poll(conn, core.gmail_for(request, conn))
        except Exception as exc:
            return core._redirect("/replies", err=f"Could not read Gmail: {type(exc).__name__}: {exc}")
    errors = f" {len(result['errors'])} thread(s) could not be read." if result["errors"] else ""
    return core._redirect("/replies", msg=f"Checked {result['threads']} thread(s): {len(result['new'])} new "
                                          f"repl{'y' if len(result['new']) == 1 else 'ies'}.{errors}")


@router.post("/replies/{reply_id}/reviewed")
def replies_reviewed(request: Request, reply_id: int):
    from . import replies
    with core._db(request) as conn:
        core._one(conn, "SELECT id FROM email_replies WHERE id=?", (reply_id,), "reply")
        replies.mark_reviewed(conn, reply_id)
    return core._redirect("/replies", msg="Marked reviewed.")


@router.post("/replies/{reply_id}/reanalyze")
def replies_reanalyze(request: Request, reply_id: int):
    with core._db(request) as conn:
        row = core._one(conn, "SELECT * FROM email_replies WHERE id=?", (reply_id,), "reply")
        bus.publish(conn, Event(type="STAKEHOLDER_REPLY_RECEIVED", entity_id=row["deal_id"],
                                dedupe_key=f"REPLY:{row['owner_id']}:{row['message_id']}:again:{now()}",
                                payload={"reply_id": reply_id, "force": True}))
        conn.commit()
    return core._redirect("/replies", msg="Re-analysis queued.")


@router.get("/automation/today", response_class=HTMLResponse)
def today_fragment(request: Request):
    """The Today sections alone, for anything that wants to refresh just them."""
    return core.templates.TemplateResponse(request, "automation_today.html",
                                           {"auto": today_data(request), "today": core.today_str()})
