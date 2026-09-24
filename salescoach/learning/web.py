"""Web routes of the learning plugin.

  GET  /learning                         "What the coach believes": patterns by family, open
                                         proposals, the outcomes panel
  POST /learning/patterns                Confirm / Wrong / Retire / Undo / Merge into / Unmerge /
                                         Do not use in prompts / Use in prompts again
  POST /learning/proposals/{id}/{accept|dismiss}
  POST /learning/recompute
  GET  /deals/{id}/outcome               the stage/status editor, a fragment deal.html loads the
                                         way it loads the intelligence fragment (static/intel.js)
  POST /deals/{id}/outcome

Every POST passes the app's same-origin guard like any other non-GET route. Every
number on these pages is a count made in code; nothing here calls a model.
"""
from contextlib import contextmanager
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..store import stores
from . import cfg, ensure_columns, feedback, outcomes, patterns, weekly

router = APIRouter()
ACTOR = "user:ui"
ACTIONS = {"confirm": "confirmed", "wrong": "wrong", "retire": "retired", "undo": None}


def _web():
    from ..web import app as webapp
    return webapp


@contextmanager
def _db(request: Request):
    conn = stores.sales(request.app.state.db_path)
    try:
        ensure_columns(conn)
        yield conn
    finally:
        conn.close()


def _redirect(url, msg=None, err=None, anchor=None):
    params = {k: v[:400] for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    return RedirectResponse(url + (f"#{anchor}" if anchor else ""), status_code=303)


def _deal_or_404(conn, deal_id):
    row = conn.execute("SELECT * FROM deals WHERE node_id=?", (deal_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "deal not found")
    return row


# ---- what the coach believes ------------------------------------------------------------------

def outcomes_panel(conn) -> dict:
    by_status = [dict(r) for r in conn.execute(
        "SELECT status, COALESCE(NULLIF(stage, ''), 'no stage') AS stage, COUNT(*) AS n FROM deals "
        "GROUP BY status, COALESCE(NULLIF(stage, ''), 'no stage') ORDER BY status, stage")]
    totals = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM deals GROUP BY status")}
    closed = []
    reasons = outcomes.lost_reasons()
    for d in conn.execute("SELECT d.node_id, d.name, d.status, d.stage, d.value, d.currency, d.lost_reason, "
                          "(SELECT MAX(changed_at) FROM deal_stage_history h WHERE h.deal_id=d.node_id "
                          " AND h.to_status=d.status) AS closed_at FROM deals d WHERE d.status IN ('won','lost') "
                          "ORDER BY closed_at DESC, d.name"):
        code, text = outcomes.lost_reason_parts(d["lost_reason"])
        closed.append({**dict(d), "reason_label": reasons.get(code, code), "reason_text": text})
    lost_by_reason: dict[str, int] = {}
    for d in closed:
        if d["status"] == "lost":
            lost_by_reason[d["reason_label"] or "no reason"] = lost_by_reason.get(d["reason_label"] or "no reason", 0) + 1
    return {"by_status": by_status, "totals": totals, "closed": closed,
            "lost_by_reason": sorted(lost_by_reason.items(), key=lambda kv: (-kv[1], kv[0])),
            "derived": outcomes.counts(conn), "n_closed": len(closed)}


def feeds_context(conn) -> dict:
    """Which prompts each pattern feeds right now, how many agent runs saw it, and what is switched on."""
    families = cfg("prompt_targets") or {}
    return {"used_in": feedback.used_in(conn), "target_labels": feedback.TARGET_LABELS,
            "feeds": [{"target": t, "label": feedback.TARGET_LABELS[t], "on": feedback.enabled(t)} for t in feedback.TARGETS],
            "fed_families": sorted({f for t in feedback.TARGETS for f in families.get(t) or []})}


def page_context(conn) -> dict:
    return {"families": patterns.beliefs(conn), "proposals": patterns.open_proposals(conn),
            "decided": patterns.decided_proposals(conn, limit=20), "digest": weekly.last_days(conn, 7),
            **feeds_context(conn),
            "series": patterns.series(conn), "outcomes": outcomes_panel(conn),
            "last_run": stores.get_user_state(conn, "learning:last_run"),
            "last_error": stores.get_user_state(conn, "learning:last_error"),
            "rate_min_n": int(cfg("followup")["rate_min_n"]),
            "reply_days": int(cfg("followup")["reply_business_days"]),
            "meeting_days": int(cfg("followup")["meeting_within_days"])}


@router.get("/learning", response_class=HTMLResponse)
def learning_page(request: Request):
    with _db(request) as conn:
        return _web().render(request, conn, "learning_page.html", **page_context(conn))


@router.post("/learning/recompute")
def learning_recompute(request: Request):
    from ..plugins import learning as plugin
    with _db(request) as conn:
        result = plugin.run_recompute(conn, trigger="manual")
    if result.get("error"):
        return _redirect("/learning", err=f"Recompute failed: {result['error']}")
    return _redirect("/learning", msg="Recomputed from the store.")


@router.post("/learning/patterns")
def pattern_action(request: Request, id: str = Form(...), action: str = Form(...), target: str = Form("")):
    with _db(request) as conn:
        try:
            if action in ACTIONS:
                patterns.set_user_state(conn, id, ACTIONS[action], by=ACTOR)
            elif action == "merge":
                if not target:
                    raise patterns.ActionRefused("Choose the pattern to merge into.")
                patterns.merge_into(conn, id, target, by=ACTOR)
            elif action == "unmerge":
                patterns.merge_into(conn, id, None, by=ACTOR)
            elif action in ("no_prompt", "use_prompt"):
                patterns.set_prompt_use(conn, id, use=action == "use_prompt", by=ACTOR)
            else:
                raise patterns.ActionRefused("Unknown action.")
        except KeyError:
            conn.rollback()
            raise HTTPException(404, "pattern not found")
        except patterns.ActionRefused as exc:
            conn.rollback()
            return _redirect("/learning", err=str(exc))
        conn.commit()
    done = {"confirm": "Confirmed. It now counts as active.", "wrong": "Marked wrong. Its observations no longer count.",
            "retire": "Retired. It will not come back on its own.", "undo": "Your verdict was removed.",
            "merge": "Merged. The counts are folded into the target.", "unmerge": "Unmerged.",
            "no_prompt": "It stays on this page and keeps being counted, but no prompt will see it.",
            "use_prompt": "It can be used in prompts again."}[action]
    return _redirect("/learning", msg=done)


@router.post("/learning/proposals/{proposal_id}/{decision}")
def proposal_decide(request: Request, proposal_id: int, decision: str):
    if decision not in ("accept", "dismiss"):
        raise HTTPException(404, "unknown decision")
    with _db(request) as conn:
        try:
            result = patterns.resolve_proposal(conn, proposal_id, accept=decision == "accept", by=ACTOR)
        except KeyError:
            conn.rollback()
            raise HTTPException(404, "proposal not found")
        except patterns.ActionRefused as exc:
            conn.rollback()
            return _redirect("/learning", err=str(exc), anchor="proposals")
        conn.commit()
    msg = {"merge": "Merged.", "config": "Accepted. The weight was written to your settings overlay.",
           None: "Dismissed."}[result["applied"]]
    return _redirect("/learning", msg=msg, anchor="proposals")


# ---- deal outcome editor (fragment on the deal page) ----------------------------------------------

def outcome_context(conn, deal_id) -> dict:
    deal = _deal_or_404(conn, deal_id)
    code, text = outcomes.lost_reason_parts(deal["lost_reason"])
    stages = list(cfg("deal").get("stages") or [])
    return {"deal": deal, "stages": stages, "statuses": outcomes.STATUSES, "lost_reasons": outcomes.lost_reasons(),
            "reason_code": code, "reason_text": text, "history": outcomes.stage_history(conn, deal_id)}


@router.get("/deals/{deal_id}/outcome", response_class=HTMLResponse)
def deal_outcome(request: Request, deal_id: str):
    with _db(request) as conn:
        return _web().templates.TemplateResponse(request, "learning_deal_outcome.html", outcome_context(conn, deal_id))


@router.post("/deals/{deal_id}/outcome")
def deal_outcome_save(request: Request, deal_id: str, stage: str = Form(""), status: str = Form(...),
                      value: str = Form(""), currency: str = Form(""), close_target: str = Form(""),
                      lost_reason_code: str = Form(""), lost_reason_text: str = Form(""), confirm: str = Form("")):
    back = f"/deals/{deal_id}"
    with _db(request) as conn:
        _deal_or_404(conn, deal_id)
        try:
            changes = {"stage": stage, "status": status, "value": value, "currency": currency,
                       "close_target": close_target}
            if status == "lost" and (lost_reason_code.strip() or lost_reason_text.strip()):
                changes["lost_reason"] = outcomes.format_lost_reason(lost_reason_code, lost_reason_text)
            result = outcomes.set_deal_outcome(conn, deal_id, changes, confirmed=confirm == "yes", by=ACTOR)
        except outcomes.OutcomeRefused as exc:
            conn.rollback()
            return _redirect(back, err=str(exc), anchor="outcome")
        conn.commit()
    return _redirect(back, msg="Saved. Nothing the agents infer will overwrite it." if result["changed"]
                     else "No changes.", anchor="outcome")
