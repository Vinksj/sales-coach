"""Web routes for sales intelligence, mounted by plugins/intelligence.py.

The core renders deal.html, deals.html and coach.html with its own context, so
the intelligence arrives as fragments those pages fetch with a few lines of
vanilla JS (static/intel.js):

  GET  /deals/{id}/intel          health, next best action, stakeholder map,
                                  methodology element grid, risks, open gate conflicts
  GET  /intel/deals.json          health + next best action per deal (Deals list)
  GET  /coach/intel               the longitudinal narrative (Coach page)
  GET  /deals/{id}/prep           the pre-call brief page

Edits are the seller's input and go through the memory gate as user_input, so
the strategist can never overwrite them. Long model work (strategy, prep,
coach report) is queued on the workflow bus and done by the worker; the
fragment refreshes itself while it waits.
"""
import json
from contextlib import contextmanager
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..orchestrator import bus
from ..schemas.events import Event
from ..store import stores
from . import coach, history, methodology, prep, tables

router = APIRouter()
ACTOR = "user"
USER = {"kind": "user_input", "ref": "deal page"}
CHOICES = {
    "position": ("champion", "supporter", "neutral", "skeptic", "blocker", "unknown"),
    "influence": ("high", "medium", "low", "unknown"),
    "champion_potential": ("high", "medium", "low", "none", "unknown"),
    "ability_to_block": ("high", "medium", "low", "unknown"),
    "relationship_strength": ("strong", "moderate", "weak", "none", "unknown"),
}
MEDDPICC_STATUS = ("known", "partial", "unknown")
RISK_STATUS = ("open", "dismissed")


def _web():
    """The core web module, imported late: the CLI and the worker load plugins without it."""
    from ..web import app as webapp
    return webapp


@contextmanager
def _db(request: Request):
    conn = stores.sales(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _redirect(url, msg=None, err=None, anchor="intel"):
    params = {k: v[:400] for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    return RedirectResponse(url + (f"#{anchor}" if anchor else ""), status_code=303)


def _deal_or_404(conn, deal_id):
    row = conn.execute("SELECT d.*, a.name AS account_name FROM deals d LEFT JOIN accounts a "
                       "ON a.node_id=d.account_id WHERE d.node_id=?", (deal_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "deal not found")
    return row


def _pending(conn, event_type, entity_id=None):
    sql = "SELECT * FROM wf_events WHERE type=? AND status IN ('pending','running')"
    params = [event_type]
    if entity_id is not None:
        sql += " AND entity_id=?"
        params.append(entity_id)
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()


def _worker_on(request) -> bool:
    thread = getattr(request.app.state, "worker_thread", None)
    return thread is not None and thread.is_alive()


def _queue(conn, event_type, entity_id, payload=None):
    bus.publish(conn, Event(type=event_type, entity_id=entity_id, payload=payload or {},
                            dedupe_key=f"{event_type}:{entity_id}:{stores.now()}"))
    conn.commit()


def _split(raw) -> list[str]:
    return [x.strip() for x in (raw or "").replace("\r", "").replace(";", "\n").split("\n") if x.strip()]


# ---- deal intelligence fragment ------------------------------------------------------

def deal_context(conn, deal_id) -> dict:
    deal = _deal_or_404(conn, deal_id)
    ok, ok_row = tables.latest_strategy(conn, deal_id)
    newest, newest_row = tables.latest_strategy(conn, deal_id, include_failed=True)
    failed = newest if newest and newest.get("failed") else None
    anchor = history.anchor_call(conn, deal_id)
    titles = {r["node_id"]: r["title"] for r in conn.execute("SELECT node_id, title FROM calls WHERE deal_id=?",
                                                              (deal_id,))}
    unmapped = [dict(r) for r in conn.execute(
        "SELECT c.node_id, c.title, COUNT(DISTINCT COALESCE(t.speaker_cluster, 'them')) AS clusters FROM calls c "
        "JOIN turns t ON t.call_id=c.node_id AND t.tier='final' AND t.channel='them' AND t.person_id IS NULL "
        "WHERE c.deal_id=? GROUP BY c.node_id ORDER BY c.started_at DESC", (deal_id,))]
    h = tables.health(conn, deal_id)
    m = methodology.active()
    med = tables.meddpicc_rows(conn, deal_id, m)
    all_risks = tables.risks(conn, deal_id, include_closed=True)
    return {
        "deal": deal, "health": h, "nba": (h or {}).get("next_best_action"),
        "stakeholders": tables.stakeholders(conn, deal_id), "meddpicc": med,
        "known": sum(row["status"] == "known" for row in med),
        "methodology": {"key": m.key, "name": m.name, "kind": m.kind, "count": m.count,
                        "columns": methodology.grid_columns(m.count)},
        "risks": [r for r in all_risks if r["status"] == "open"],
        "closed_risks": [r for r in all_risks if r["status"] != "open"],
        "conflicts": tables.conflicts(conn, deal_id), "strategy": ok, "strategy_row": ok_row,
        "failed": failed, "failed_row": newest_row if failed else None,
        "pending": _pending(conn, "STRATEGY_REQUESTED", deal_id), "anchor": anchor, "titles": titles,
        "unmapped": unmapped, "choices": CHOICES, "meddpicc_status": MEDDPICC_STATUS,
    }


@router.get("/deals/{deal_id}/intel", response_class=HTMLResponse)
def deal_intel(request: Request, deal_id: str):
    with _db(request) as conn:
        ctx = deal_context(conn, deal_id)
        ctx["worker_on"] = _worker_on(request)
        return _web().templates.TemplateResponse(request, "intel_deal.html", ctx)


@router.get("/intel/deals.json")
def deals_json(request: Request):
    with _db(request) as conn:
        out = {}
        for r in conn.execute("SELECT deal_id, score, label, next_best_action, updated_at FROM deal_health"):
            nba = tables._loads(r["next_best_action"], None) or {}
            out[r["deal_id"]] = {"score": r["score"], "label": r["label"], "updated_at": r["updated_at"],
                                 "next_best_action": nba.get("action"), "by_when": nba.get("by_when"),
                                 "owner": nba.get("owner")}
        return JSONResponse(out)


@router.post("/deals/{deal_id}/strategy")
def deal_strategy(request: Request, deal_id: str):
    with _db(request) as conn:
        _deal_or_404(conn, deal_id)
        if history.anchor_call(conn, deal_id) is None:
            return _redirect(f"/deals/{deal_id}", err="No analysed call on this deal yet; the strategist needs one.")
        _queue(conn, "STRATEGY_REQUESTED", deal_id, {"force": True})
    tail = "" if _worker_on(request) else f" The worker is not running here; run: salescoach strategy --deal {deal_id}"
    return _redirect(f"/deals/{deal_id}", msg="Strategy run queued. This section refreshes when it is done." + tail)


@router.post("/deals/{deal_id}/stakeholders/{person_id}")
def stakeholder_edit(request: Request, deal_id: str, person_id: str, role: str = Form(""), position: str = Form(""),
                     influence: str = Form(""), champion_potential: str = Form(""), ability_to_block: str = Form(""),
                     relationship_strength: str = Form(""), incentives: str = Form(""), concerns: str = Form("")):
    with _db(request) as conn:
        _deal_or_404(conn, deal_id)
        if not conn.execute("SELECT 1 FROM deal_people WHERE deal_id=? AND person_id=?", (deal_id, person_id)).fetchone():
            raise HTTPException(404, "that person is not on this deal")
        wanted = {}
        for field, value in (("position", position), ("influence", influence), ("champion_potential", champion_potential),
                             ("ability_to_block", ability_to_block), ("relationship_strength", relationship_strength)):
            if value:
                if value not in CHOICES[field]:
                    return _redirect(f"/deals/{deal_id}", err=f"{field.replace('_', ' ')} must be one of "
                                                               f"{', '.join(CHOICES[field])}")
                wanted[field] = value
        if role.strip():
            wanted["role"] = role.strip()
        wanted["incentives"], wanted["concerns"] = _split(incentives), _split(concerns)
        sid = tables.stakeholder_id(deal_id, person_id)
        cur = conn.execute("SELECT * FROM stakeholders WHERE id=?", (sid,)).fetchone()

        def same(field, value):
            if cur is None:
                return value in (None, "", [])
            have = cur[field]
            return (tables._loads(have, []) or []) == value if isinstance(value, list) else have == value

        changed = {k: v for k, v in wanted.items() if not same(k, v)}
        if changed:
            tables.upsert(conn, "stakeholders", sid, {"deal_id": deal_id, "person_id": person_id}, changed,
                          "user_input", USER, {"updated_at": stores.now()}, actor=ACTOR)
        conn.commit()
    return _redirect(f"/deals/{deal_id}", msg="Saved. The strategist will not overwrite what you set."
                     if changed else "No changes.", anchor=f"stake-{person_id}")


@router.post("/deals/{deal_id}/meddpicc/{element}")
def meddpicc_edit(request: Request, deal_id: str, element: str, status: str = Form(...),
                  what_we_know: str = Form(None), gap: str = Form(None), next_question: str = Form(None)):
    active = methodology.active()
    if element not in active.keys:
        raise HTTPException(404, f"unknown {active.name} element")
    if status not in MEDDPICC_STATUS:
        return _redirect(f"/deals/{deal_id}", err=f"status must be one of {', '.join(MEDDPICC_STATUS)}")
    with _db(request) as conn:
        _deal_or_404(conn, deal_id)
        mid = tables.meddpicc_id(deal_id, element)
        cur = conn.execute("SELECT * FROM meddpicc WHERE id=?", (mid,)).fetchone()
        wanted = {"status": status}
        for field, value in (("what_we_know", what_we_know), ("gap", gap), ("next_question", next_question)):
            if value is not None:
                wanted[field] = value.strip()
        changed = {k: v for k, v in wanted.items() if cur is None or (cur[k] or "") != v}
        if changed:
            tables.upsert(conn, "meddpicc", mid, {"deal_id": deal_id, "element": element}, changed, "user_input", USER,
                          {"updated_at": stores.now()}, actor=ACTOR)
        conn.commit()
    return _redirect(f"/deals/{deal_id}", msg=f"{active.label(element)} saved." if changed else "No changes.",
                     anchor=f"m-{element}")


@router.post("/deals/{deal_id}/risks/{risk_type}/status")
def risk_status(request: Request, deal_id: str, risk_type: str, status: str = Form(...)):
    if status not in RISK_STATUS:
        return _redirect(f"/deals/{deal_id}", err="status must be open or dismissed")
    with _db(request) as conn:
        _deal_or_404(conn, deal_id)
        rid = tables.risk_id(deal_id, risk_type)
        tables.migrate_risk_ids(conn)
        if not conn.execute("SELECT 1 FROM deal_risks WHERE id=?", (rid,)).fetchone():
            raise HTTPException(404, "risk not found")
        tables.upsert(conn, "deal_risks", rid, {}, {"status": status}, "user_input", USER,
                      {"updated_at": stores.now()}, actor=ACTOR)
        conn.commit()
    return _redirect(f"/deals/{deal_id}", msg="Risk dismissed." if status == "dismissed" else "Risk reopened.")


# ---- prep brief -----------------------------------------------------------------------

@router.get("/deals/{deal_id}/prep", response_class=HTMLResponse)
def prep_page(request: Request, deal_id: str, id: int | None = None):
    with _db(request) as conn:
        deal = _deal_or_404(conn, deal_id)
        brief = prep.get(conn, brief_id=id) if id else prep.get(conn, deal_id=deal_id)
        if brief and brief.get("deal_id") != deal_id:
            raise HTTPException(404, "brief not found for this deal")
        earlier = conn.execute("SELECT id, meeting_title, meeting_at, created_at FROM prep_briefs WHERE deal_id=? "
                               "ORDER BY id DESC LIMIT 10", (deal_id,)).fetchall()
        pending = _pending(conn, "PREP_REQUESTED", deal_id)
        titles = {r["node_id"]: r["title"] for r in conn.execute("SELECT node_id, title FROM calls")}
        return _web().render(request, conn, "intel_prep.html", deal=deal, brief=brief, earlier=earlier,
                             pending=pending, titles=titles, worker_on=_worker_on(request),
                             methodology_name=prep.methodology_name(brief or {}) if brief
                             else methodology.active().name, autorefresh=5 if pending else 0)


@router.post("/deals/{deal_id}/prep")
def prep_request(request: Request, deal_id: str, title: str = Form(""), attendees: str = Form(""),
                 when: str = Form("")):
    with _db(request) as conn:
        _deal_or_404(conn, deal_id)
        people = [a.strip() for a in attendees.replace(";", ",").replace("\n", ",").split(",") if a.strip()]
        try:
            when_iso = _web()._started_at(when)      # a bare datetime-local value is IST, not UTC
        except ValueError:
            return _redirect(f"/deals/{deal_id}/prep", err="Meeting time is not a valid date.", anchor=None)
        _queue(conn, "PREP_REQUESTED", deal_id, {"title": title.strip() or None, "attendees": people,
                                                  "when": when_iso})
    tail = "" if _worker_on(request) else f" The worker is not running here; run: salescoach prep --deal {deal_id}"
    return _redirect(f"/deals/{deal_id}/prep", msg="Preparing the brief. This page refreshes when it is ready." + tail,
                     anchor=None)


# ---- coach --------------------------------------------------------------------------------

@router.get("/coach/intel", response_class=HTMLResponse)
def coach_intel(request: Request):
    with _db(request) as conn:
        report = coach.latest(conn)
        titles = {r["node_id"]: r["title"] for r in conn.execute("SELECT node_id, title FROM calls")}
        n = len(coach.analysed_calls(conn))
        error = (stores.get_state(conn, "intel:coach_error") or "").strip()
        return _web().templates.TemplateResponse(request, "intel_coach.html", {
            "report": report, "titles": titles, "n_calls": n,
            "min_calls": int((history.cfg().get("coach") or {}).get("min_calls", 3)),
            "pending": _pending(conn, "COACH_REPORT_REQUESTED"), "error": error, "worker_on": _worker_on(request)})


@router.post("/coach/report")
def coach_request(request: Request):
    with _db(request) as conn:
        _queue(conn, "COACH_REPORT_REQUESTED", "coach")
    tail = "" if _worker_on(request) else " The worker is not running here; run: salescoach coach-report"
    return _redirect("/coach", msg="Coach report queued." + tail, anchor="coach-intel")


def as_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)
