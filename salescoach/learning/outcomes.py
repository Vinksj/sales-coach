"""Outcome ground truth. Without it the system would learn from a model's opinion.

Two halves:

  set_deal_outcome()   the user's stage/status/value edits on a deal. Every field goes
                       through the memory gate as user_input and a stage or status
                       change appends a deal_stage_history row. Won/lost needs an
                       explicit confirm; lost needs a reason.

  recompute()          derived_outcomes, rebuilt from rows already in the store. Code
                       only, idempotent (UNIQUE(kind, subject_type, subject_id); a row is
                       rewritten only when its value or details changed):

    email_replied        a buyer reply in the email's thread within N business days
    meeting_after_email  a calendar meeting on the same deal first seen within N days
                         after the email went out
    loop_closed_on_time  the loop was done on or before its due date
    call_advanced        facts only, against the previous call on the same deal: a new
                         buyer-side participant or stakeholder, a buyer-owned loop closed
                         on time, or a methodology element that went unknown -> known/
                         partial (events audit trail). Never a model's "advanced" verdict.
                         A row the strategist merely CREATED counts only when the deal's
                         previous strategist run used the same methodology: the first run
                         after a switch creates rows for the new framework's elements, and
                         that is a change of table, not something learned on the call.

  value: 1 happened, 0 did not, NULL still inside its window (too early to count).
"""
import json
from datetime import date, timedelta

from ..automation import common
from ..execution.cadence import add_business_days
from ..memory import gate
from ..store.stores import now
from . import cfg, ensure_columns

STATUSES = ("active", "paused", "won", "lost")
CLOSED = ("won", "lost")
USER = {"kind": "user_input", "ref": "deal outcome"}
KINDS = ("email_replied", "meeting_after_email", "loop_closed_on_time", "call_advanced")


class OutcomeRefused(ValueError):
    """The edit is not allowed as submitted; the message is for the user."""


# ---- the user's outcome edits ---------------------------------------------------------

def lost_reasons() -> dict:
    return dict(cfg("deal").get("lost_reasons") or {"other": "Other"})


def format_lost_reason(code: str | None, text: str | None) -> str | None:
    code, text = (code or "").strip(), " ".join((text or "").split())[:300]
    if code and code not in lost_reasons():
        raise OutcomeRefused("Pick a lost reason from the list.")
    if code == "other" and not text:
        raise OutcomeRefused("\"Other\" needs a few words on why the deal was lost.")
    if not code and not text:
        return None
    return f"{code}: {text}" if code and text else (code or f"other: {text}")


def lost_reason_parts(raw: str | None) -> tuple[str, str]:
    """(code, free text) of a stored lost_reason."""
    if not raw:
        return "", ""
    code, sep, text = raw.partition(": ")
    return (code, text) if sep else (raw, "")


def _clean(changes: dict) -> dict:
    out = {}
    for field, raw in changes.items():
        if field not in ("stage", "status", "value", "currency", "close_target", "lost_reason"):
            raise OutcomeRefused(f"{field} is not an outcome field")
        value = raw.strip() if isinstance(raw, str) else raw
        value = None if value == "" else value
        if field == "status":
            if value not in STATUSES:
                raise OutcomeRefused(f"Status must be one of {', '.join(STATUSES)}.")
        elif field == "value" and value is not None:
            try:
                value = float(str(value).replace(",", ""))
            except ValueError:
                raise OutcomeRefused("Deal value must be a number.")
            if value < 0 or value != value or value in (float("inf"),):
                raise OutcomeRefused("Deal value must be zero or more.")
        elif field == "close_target" and value is not None:
            try:
                value = date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError:
                raise OutcomeRefused("Close target must be a date (YYYY-MM-DD).")
        elif field == "currency" and value is not None:
            value = str(value).upper()[:8]
        elif field == "stage" and value is not None:
            value = " ".join(str(value).split())[:60]
        out[field] = value
    return out


def set_deal_outcome(conn, deal_id: str, changes: dict, confirmed: bool = False, by: str = "user:ui") -> dict:
    """Apply the user's outcome edit. Returns {"changed": [...], "history_id": int|None}.

    Raises KeyError for an unknown deal and OutcomeRefused when won/lost is not confirmed
    or a lost deal has no reason. Nothing is written when it raises.
    """
    ensure_columns(conn)
    deal = conn.execute("SELECT * FROM deals WHERE node_id=?", (deal_id,)).fetchone()
    if deal is None:
        raise KeyError(deal_id)
    wanted = _clean(changes)
    status = wanted.get("status", deal["status"])
    if status in CLOSED and status != deal["status"] and not confirmed:
        raise OutcomeRefused(f"Marking a deal {status} needs the confirm box ticked.")
    if status == "lost":
        if not (wanted["lost_reason"] if "lost_reason" in wanted else deal["lost_reason"]):
            raise OutcomeRefused("A lost deal needs a reason.")
    elif deal["lost_reason"] or wanted.get("lost_reason"):
        wanted["lost_reason"] = None                  # the reason stays in the history row, not on a live deal

    changed = [f for f, v in wanted.items() if deal[f] != v]
    for field in changed:
        gate.propose(conn, gate.Proposed(deal_id, "deals", field, wanted[field], "user_input", dict(USER)), actor=by)
    history_id = None
    if "stage" in changed or "status" in changed:
        cur = conn.execute(
            "INSERT INTO deal_stage_history(deal_id,from_stage,to_stage,from_status,to_status,lost_reason,changed_at,\"by\") "
            "VALUES (?,?,?,?,?,?,?,?)",
            (deal_id, deal["stage"], wanted.get("stage", deal["stage"]), deal["status"], status,
             wanted.get("lost_reason", deal["lost_reason"]) if status == "lost" else None, now(), by))
        history_id = cur.lastrowid
    if changed:
        conn.execute("UPDATE deals SET updated_at=? WHERE node_id=?", (now(), deal_id))
    return {"changed": changed, "history_id": history_id}


def stage_history(conn, deal_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM deal_stage_history WHERE deal_id=? ORDER BY id DESC", (deal_id,))]


# ---- derived outcomes -----------------------------------------------------------------

class _Writer:
    """Upserts that leave an unchanged row (and its computed_at) alone, and drop rows whose subject
    no longer qualifies, so running recompute twice is a no-op."""

    def __init__(self, conn):
        self.conn, self.seen, self.written = conn, set(), 0
        self.have = {(r["kind"], r["subject_type"], r["subject_id"]): r
                     for r in conn.execute("SELECT * FROM derived_outcomes")}

    def put(self, kind, subject_type, subject_id, deal_id, value, details: dict):
        key = (kind, subject_type, str(subject_id))
        self.seen.add(key)
        blob = json.dumps(details, sort_keys=True, default=str)
        old = self.have.get(key)
        if old is not None and old["value"] == value and old["details"] == blob and old["deal_id"] == deal_id:
            return
        self.conn.execute(
            "INSERT INTO derived_outcomes(kind,subject_type,subject_id,deal_id,value,computed_at,details) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,subject_type,subject_id) DO UPDATE SET "
            "deal_id=excluded.deal_id, value=excluded.value, computed_at=excluded.computed_at, details=excluded.details",
            (*key, deal_id, value, now(), blob))
        self.written += 1

    def prune(self) -> int:
        stale = [k for k in self.have if k not in self.seen and k[0] in KINDS]
        for k in stale:
            self.conn.execute("DELETE FROM derived_outcomes WHERE kind=? AND subject_type=? AND subject_id=?", k)
        return len(stale)


def business_deadline(sent: date, days: int) -> date:
    """The last day a reply still counts: N business days after the send date (execution/cadence.py)."""
    return add_business_days(sent, days)


def _sent_emails(conn):
    return conn.execute("SELECT * FROM emails WHERE status='sent' AND sent_at IS NOT NULL ORDER BY sent_at, id").fetchall()


def _email_replied(conn, w: _Writer, today: date, emails) -> None:
    days = int(cfg("followup").get("reply_business_days", 5))
    by_thread: dict[str, list] = {}
    for e in emails:
        if e["gmail_thread_id"]:
            by_thread.setdefault(e["gmail_thread_id"], []).append(e)
    credited: dict[int, list] = {}
    for r in conn.execute("SELECT id, thread_id, email_id, received_at FROM email_replies ORDER BY received_at, id"):
        got = common.ts(r["received_at"])
        ours = [e for e in by_thread.get(r["thread_id"], []) if got and common.ts(e["sent_at"]) <= got]
        # A reply answers the latest email of ours that was already out when it arrived.
        email_id = ours[-1]["id"] if ours else r["email_id"]
        if email_id is not None:
            credited.setdefault(email_id, []).append(r)
    for e in emails:
        sent_day = common.ist_date(e["sent_at"])
        deadline = business_deadline(sent_day, days)
        in_time = [r for r in credited.get(e["id"], [])
                   if common.ts(r["received_at"]) and common.ts(r["received_at"]) >= common.ts(e["sent_at"])
                   and common.ist_date(r["received_at"]) <= deadline]
        late = [r for r in credited.get(e["id"], []) if r not in in_time]
        value = 1 if in_time else (None if today <= deadline else 0)
        w.put("email_replied", "email", e["id"], e["deal_id"], value, {
            "sent_on": sent_day.isoformat(), "deadline": deadline.isoformat(), "business_days": days,
            "reply_ids": [r["id"] for r in in_time], "late_reply_ids": [r["id"] for r in late], "email_kind": e["kind"]})


def _meeting_after_email(conn, w: _Writer, today: date, emails) -> None:
    days = int(cfg("followup").get("meeting_within_days", 10))
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calendar_meetings'").fetchone():
        return
    meetings: dict[str, list] = {}
    for m in conn.execute("SELECT event_id, deal_id, start_at, first_seen_at FROM calendar_meetings "
                          "WHERE deal_id IS NOT NULL ORDER BY first_seen_at"):
        meetings.setdefault(m["deal_id"], []).append(m)
    for e in emails:
        if not e["deal_id"]:
            continue
        sent = common.ts(e["sent_at"])
        until = sent + timedelta(days=days)
        hit = next((m for m in meetings.get(e["deal_id"], [])
                    if common.ts(m["first_seen_at"]) and sent < common.ts(m["first_seen_at"]) <= until
                    and (common.ts(m["start_at"]) is None or common.ts(m["start_at"]) > sent)), None)
        value = 1 if hit else (None if today <= common.ist_date(until) else 0)
        w.put("meeting_after_email", "email", e["id"], e["deal_id"], value, {
            "within_days": days, "event_id": hit["event_id"] if hit else None,
            "first_seen_at": hit["first_seen_at"] if hit else None, "email_kind": e["kind"]})


def _loop_on_time(conn, w: _Writer, today: date) -> dict:
    """Returns {loop_id: 1|0|None} for the call_advanced pass."""
    verdicts = {}
    for l in conn.execute("SELECT node_id, deal_id, owner, status, due_date, closed_at FROM loops "
                          "WHERE due_date IS NOT NULL AND review_state!='rejected' "
                          "AND status IN ('open','waiting','done')"):
        try:
            due = date.fromisoformat(l["due_date"][:10])
        except ValueError:
            continue
        closed = common.ist_date(l["closed_at"]) if l["status"] == "done" else None
        if l["status"] == "done":
            value = 1 if closed is not None and closed <= due else 0
        else:
            value = None if today <= due else 0
        verdicts[l["node_id"]] = value
        w.put("loop_closed_on_time", "loop", l["node_id"], l["deal_id"], value, {
            "owner": l["owner"], "due_date": due.isoformat(), "closed_on": closed.isoformat() if closed else None})
    return verdicts


def _own_domains(conn) -> set:
    own = {str(d).lower() for d in (common.cfg("calendar").get("own_domains") or [])}
    return own | {a.rsplit("@", 1)[-1] for a in common.my_addresses(conn) if "@" in a}


def _buyer_side(conn, call_id, own) -> dict:
    out = {}
    for p in conn.execute("SELECT p.node_id, p.email FROM call_participants cp JOIN people p ON p.node_id=cp.person_id "
                          "WHERE cp.call_id=? AND p.is_me=0", (call_id,)):
        domain = (p["email"] or "").rsplit("@", 1)[-1].lower() if p["email"] else ""
        if not domain or domain not in own:
            out[p["node_id"]] = True
    return out


DEFAULT_METHODOLOGY = "meddpicc"            # what every strategist run before methodologies were data used


def _strategist_runs(conn) -> dict:
    """{deal_id: [(run_id, methodology key)]}, oldest first: the successful strategist runs of each deal."""
    out: dict[str, list] = {}
    for r in conn.execute("SELECT r.id, r.input_refs, c.deal_id FROM agent_runs r LEFT JOIN calls c ON c.node_id=r.call_id "
                          "WHERE r.agent='deal_strategist' AND r.status='ok' ORDER BY r.id"):
        try:
            refs = json.loads(r["input_refs"] or "{}")
        except ValueError:
            refs = {}
        deal_id = refs.get("deal_id") or r["deal_id"]
        if deal_id:
            out.setdefault(deal_id, []).append((r["id"], refs.get("methodology") or DEFAULT_METHODOLOGY))
    return out


def _created_under_same_methodology(ev, runs: list) -> bool:
    """A `meddpicc_created` event: was the element new to the DEAL, or only new to the table? True when
    the run that created the row was preceded by a run of the same methodology (so the framework did not
    change under it), and for a row made by hand. False for the first run after a switch, and for the
    deal's very first run (no earlier read to have advanced from)."""
    source = ev["source_id"] or ""
    if not source.startswith("run:"):
        return True
    try:
        run_id = int(source.split(":", 1)[1])
    except ValueError:
        return True
    this = next((m for rid, m in runs if rid == run_id), None)
    before = [m for rid, m in runs if rid < run_id]
    return this is not None and bool(before) and before[-1] == this


def _call_advanced(conn, w: _Writer, loop_verdicts: dict) -> None:
    own = _own_domains(conn)
    analysed = {r[0] for r in conn.execute("SELECT DISTINCT call_id FROM artifacts WHERE kind='analysis'")}
    run_call = {f"run:{r['id']}": r["call_id"] for r in conn.execute(
        "SELECT id, call_id FROM agent_runs WHERE call_id IS NOT NULL")}
    strategist_runs = _strategist_runs(conn)
    by_deal: dict[str, list] = {}
    for c in conn.execute("SELECT node_id, deal_id, started_at FROM calls WHERE deal_id IS NOT NULL "
                          "AND started_at IS NOT NULL ORDER BY started_at, node_id"):
        by_deal.setdefault(c["deal_id"], []).append(c)
    has_intel = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meddpicc'").fetchone())

    for deal_id, calls in by_deal.items():
        element_events, stakeholder_events = [], []
        if has_intel:
            for ev in conn.execute("SELECT ts, kind, node_id, before, after, source_id FROM events WHERE node_id LIKE ? "
                                   "AND kind IN ('meddpicc_status_changed','meddpicc_created','stakeholders_created') "
                                   "ORDER BY id", (f"{deal_id}:%",)):
                (stakeholder_events if ev["kind"] == "stakeholders_created" else element_events).append(ev)
        loops = conn.execute("SELECT node_id, call_id, owner, closed_at FROM loops WHERE deal_id=? AND owner='prospect' "
                             "AND status='done'", (deal_id,)).fetchall()
        loop_prov = {r["entity_id"]: json.loads(r["provenance"] or "{}") for r in conn.execute(
            "SELECT entity_id, provenance FROM field_provenance WHERE field='status' AND entity_id IN "
            "(SELECT node_id FROM loops WHERE deal_id=?)", (deal_id,))}
        seen_people: set = set()
        for i, call in enumerate(calls):
            here = _buyer_side(conn, call["node_id"], own)
            if i == 0 or call["node_id"] not in analysed:
                seen_people |= set(here)
                continue
            prev = calls[i - 1]
            t0, t1 = common.ts(prev["started_at"]), common.ts(call["started_at"])

            def mine(ev) -> bool:
                """Caused by this call's own agent run, or made by hand between the two calls."""
                if ev["source_id"] in run_call:
                    return run_call[ev["source_id"]] == call["node_id"]
                at = common.ts(ev["ts"])
                return at is not None and t0 < at <= t1

            new_people = sorted(p for p in here if p not in seen_people)
            new_stakeholders = sorted({ev["node_id"].split(":", 1)[1] for ev in stakeholder_events if mine(ev)}
                                      - set(new_people) - seen_people)
            elements, switched = [], []
            for ev in element_events:
                before = json.loads(ev["before"] or "{}").get("status") or "unknown"
                after = json.loads(ev["after"] or "{}").get("status")
                if before == "unknown" and after in ("known", "partial") and mine(ev):
                    element = ev["node_id"].split(":", 1)[1]
                    # A status change means the row was already there: the element went unknown -> known.
                    # A creation is an advance only if the framework did not change under it.
                    if ev["kind"] == "meddpicc_created" and \
                            not _created_under_same_methodology(ev, strategist_runs.get(deal_id, [])):
                        switched.append(element)
                    else:
                        elements.append({"element": element, "to": after})
            closed = []
            for l in loops:
                if l["call_id"] == call["node_id"] or loop_verdicts.get(l["node_id"]) != 1:
                    continue
                prov = loop_prov.get(l["node_id"], {})
                at = common.ts(l["closed_at"])
                # Reported done on this call (the gate's provenance names it), or closed between the two calls.
                if (prov.get("kind") == "call" and prov.get("ref") == call["node_id"]) or \
                        (at is not None and t0 < at <= t1):
                    closed.append(l["node_id"])
            reasons = {"new_participants": new_people, "new_stakeholders": new_stakeholders,
                       "buyer_loops_closed_on_time": sorted(closed), "elements_now_known": elements}
            details = {"previous_call": prev["node_id"], **reasons}
            if switched:                                # shown, never counted (only written when there is one,
                details["elements_created_not_counted"] = sorted(switched)    # so older rows are not rewritten)
            w.put("call_advanced", "call", call["node_id"], deal_id, 1 if any(reasons.values()) else 0, details)
            seen_people |= set(here)


def recompute(conn, today: date | None = None) -> dict:
    """Rebuild derived_outcomes. Safe to call any number of times; does not commit."""
    today = today or common.today_ist()
    w = _Writer(conn)
    emails = _sent_emails(conn)
    _email_replied(conn, w, today, emails)
    _meeting_after_email(conn, w, today, emails)
    verdicts = _loop_on_time(conn, w, today)
    _call_advanced(conn, w, verdicts)
    return {"written": w.written, "removed": w.prune(), "rows": len(w.seen)}


def counts(conn) -> list[dict]:
    """Per kind: how many happened, did not, or are still open. For the Outcomes panel."""
    out = []
    for kind in KINDS:
        row = conn.execute("SELECT SUM(value=1) AS yes, SUM(value=0) AS no, SUM(value IS NULL) AS pending, COUNT(*) AS n "
                           "FROM derived_outcomes WHERE kind=?", (kind,)).fetchone()
        out.append({"kind": kind, "yes": row["yes"] or 0, "no": row["no"] or 0, "pending": row["pending"] or 0,
                    "n": row["n"] or 0})
    return out
