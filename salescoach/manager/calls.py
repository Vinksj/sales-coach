"""/calls: every call the viewer may read, with filters.

Row-level security decides whose calls these are (a rep: their own; a manager: their own and their
team's). The query adds an owner condition only when the viewer picks a rep. The same predicates
back the numbers on /team (team.py), so a number there and the list it links to always agree.
"""
from datetime import date

from .. import budget
from ..orchestrator import workflow

# state filter -> (label, SQL on calls c)
DONE_STATES = ("done", "email_sent", "email_skipped")
# A step deferred by the daily model budget is waiting, not failed (budget.is_waiting): it counts as processing.
BUDGET_WAIT_SQL = f"c.wf_error LIKE '%: {budget.WAIT_MARK}: %'"
STATES = {
    "awaiting_review": ("Awaiting review", "c.wf_state='awaiting_review' AND c.wf_error IS NULL"),
    "reviewed": ("Reviewed", "c.wf_state='reviewed' AND c.wf_error IS NULL"),
    "done": ("Done", "c.wf_state IN ('done','email_sent','email_skipped') AND c.wf_error IS NULL"),
    "failed": ("Failed", f"((c.wf_error IS NOT NULL AND NOT ({BUDGET_WAIT_SQL})) OR c.wf_state='capture_failed')"),
    "needs_speaker": ("Needs the rep: which speaker", "c.wf_state IN ({holds}) AND c.wf_error IS NULL"),
    "processing": ("Processing", f"(c.wf_error IS NULL OR {BUDGET_WAIT_SQL}) AND c.wf_state NOT IN "
                                 "('awaiting_review','reviewed','done','email_sent','email_skipped','capture_failed','live',"
                                 "{holds})"),
}
EMAIL = {
    "drafted": ("A follow-up was drafted", "EXISTS (SELECT 1 FROM emails e WHERE e.call_id=c.node_id "
                                           "AND e.kind='followup')"),
    "sent": ("A follow-up was sent", "EXISTS (SELECT 1 FROM emails e WHERE e.call_id=c.node_id "
                                     "AND e.kind='followup' AND e.status='sent')"),
}
# A methodology gap: the call's deal is open and the element is neither known nor partial there.
GAP_SQL = ("c.deal_id IS NOT NULL AND EXISTS (SELECT 1 FROM deals g WHERE g.node_id=c.deal_id AND g.status='active') "
           "AND NOT EXISTS (SELECT 1 FROM meddpicc m WHERE m.deal_id=c.deal_id AND m.element=? "
           "AND m.status IN ('known','partial'))")
LIMIT = 300


def state_sql(key: str) -> str:
    holds = ",".join(f"'{s}'" for s in workflow.HOLD_STATES)
    return STATES[key][1].format(holds=holds)


def _day(raw) -> str:
    raw = (raw or "").strip()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


def clean_filters(qp, gap_keys=()) -> dict:
    """The filters from a query string, each one validated (anything unknown is dropped)."""
    f = {"rep": (qp.get("rep") or "").strip(), "from": _day(qp.get("from")), "to": _day(qp.get("to")),
         "deal": (qp.get("deal") or "").strip(), "state": qp.get("state") or "", "gap": qp.get("gap") or "",
         "email": qp.get("email") or ""}
    if f["state"] not in STATES:
        f["state"] = ""
    if f["email"] not in EMAIL:
        f["email"] = ""
    if f["gap"] not in gap_keys:
        f["gap"] = ""
    return f


def where(f: dict) -> tuple:
    """(SQL conditions joined with AND, params) for the filters. No owner condition unless f["rep"]."""
    conds, params = ["1=1"], []
    if f.get("rep"):
        conds.append("c.owner_id=?")
        params.append(f["rep"])
    if f.get("from"):
        conds.append("substr(c.started_at,1,10)>=?")
        params.append(f["from"])
    if f.get("to"):
        conds.append("substr(c.started_at,1,10)<=?")
        params.append(f["to"])
    if f.get("deal"):
        conds.append("c.deal_id=?")
        params.append(f["deal"])
    if f.get("state"):
        conds.append(state_sql(f["state"]))
    if f.get("email"):
        conds.append(EMAIL[f["email"]][1])
    if f.get("gap"):
        conds.append(GAP_SQL)
        params.append(f["gap"])
    return " AND ".join(conds), params


def query(conn, f: dict, limit: int = LIMIT) -> list:
    sql, params = where(f)
    return [dict(r) for r in conn.execute(
        "SELECT c.node_id, c.title, c.started_at, c.owner_id, c.deal_id, c.wf_state, c.wf_error, c.source, "
        "d.name AS deal_name FROM calls c LEFT JOIN deals d ON d.node_id=c.deal_id "
        f"WHERE {sql} ORDER BY c.started_at DESC, c.node_id LIMIT ?", (*params, int(limit))).fetchall()]


def count(conn, f: dict) -> int:
    sql, params = where(f)
    return conn.execute(f"SELECT COUNT(*) FROM calls c WHERE {sql}", params).fetchone()[0]


def state_label(call: dict) -> str:
    for key in ("failed", "needs_speaker", "awaiting_review", "reviewed", "done"):
        if _matches(call, key):
            return STATES[key][0]
    if budget.is_waiting(call.get("wf_error")):
        return "Waiting for tomorrow's model budget"
    return "Live" if call.get("wf_state") == "live" else STATES["processing"][0]


def _matches(call: dict, key: str) -> bool:
    state, err = call.get("wf_state"), call.get("wf_error")
    if key == "failed":
        return (bool(err) and not budget.is_waiting(err)) or state == "capture_failed"
    if err:
        return False
    return {"needs_speaker": state in workflow.HOLD_STATES, "awaiting_review": state == "awaiting_review",
            "reviewed": state == "reviewed", "done": state in DONE_STATES}[key]
