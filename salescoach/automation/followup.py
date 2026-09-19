"""The follow-up agent: for each due open loop, decide whether to nudge, wait,
escalate, close or ask the seller (spec §10-11).

It never reminds blindly. For each due loop:
  1. Deterministic checks, the most certain first:
       completed    an open proposal already says the loop is done, cancelled or superseded;
       superseded   the loop was replaced, or its deal is won, lost or paused;
       replied      its owner wrote to us since the loop last moved;
       newer call   we spoke to the account since then and that call did not touch it;
       own item     it is the seller's own commitment (nothing to send the buyer);
       pending      a nudge for it is drafted and waiting for his Send;
       escalation   follow_up_count reached the limit: a deal_risk loop, no more nudges;
       too soon     the last nudge went out too recently;
       no recipient, weekend.
  2. Only then the follow-up agent (an LLM) answers send_nudge | wait_until |
     escalate | close_as_stale | ask_user, with a rationale and a read on
     relationship risk. High risk turns send_nudge into ask_user.
  3. send_nudge drafts the nudge with a second agent, in his voice, addressed
     only to the deal's people. It is an ordinary emails row (kind 'nudge',
     status 'drafted'), so it waits for his Send like every other email.

follow_up_count moves only when a nudge is actually SENT (on_email_sent).
Nothing here closes a loop: close_as_stale parks a proposal in the memory gate
for the seller. Every decision lands in followup_decisions with an agent_runs row
(rule-made ones too), so "why did the system send this follow-up?" always has
an answer (why()).
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from .. import config, repo, seller
from ..agents.base import AgentFailed, record_items
from ..execution import cadence
from ..memory import gate
from ..orchestrator import bus
from ..schemas.events import Event
from ..store.stores import engine, now, set_state
from ..validators import recipients, voice_lint
from . import common
from .schemas import FollowupDecision, NudgeDraft

ACTOR = "followup"
PENDING_NUDGE = ("drafted", "approved", "sending", "failed", "saved_to_gmail")
PASSIVE_AGGRESSIVE = (
    "just following up", "just checking in", "gentle reminder", "friendly reminder", "circling back",
    "as per my last", "per my previous", "per my last email", "i haven't heard back", "i have not heard back",
    "bumping this", "any update?", "as discussed earlier", "still waiting",
)
PRIORITY_SQL = "CASE l.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
LOOP_SELECT = ("SELECT l.*, d.name AS deal_name, d.status AS deal_status, d.stage AS deal_stage, "
               "d.next_step AS deal_next_step FROM loops l LEFT JOIN deals d ON d.node_id=l.deal_id ")
TRACKED = "(l.review_state='confirmed' OR l.source='world') AND l.review_state!='rejected'"


class NoRecipient(RuntimeError):
    pass


# followup_decisions.stage for "the user asked for this" (migration 5 renamed the value an older
# build stored). No page shows the raw value (web filter `who`).
USER_STAGE = "user"


class FollowupAgent(common.PluginAgent):
    name = "followup"
    schema = FollowupDecision


class NudgeAgent(common.PluginAgent):
    name = "nudge"
    schema = NudgeDraft

    def system_prompt(self, ctx):
        return super().system_prompt(ctx) + "\n\n## Style guide\n" + seller.render(config.text("style.md"))


@dataclass
class Outcome:
    check: str
    decision: str
    rationale: str
    next_check: Optional[date] = None
    wait_until: Optional[date] = None
    park: Optional[tuple] = None             # (status, confidence) proposed to the seller through the gate
    activity: Optional[str] = None           # new last_activity_at, so the same fact is not re-raised
    risk: Optional[str] = None
    note: Optional[str] = None
    run_id: Optional[int] = None


# ---- small rules -----------------------------------------------------------------

def nudge_lint(body: str) -> list[dict]:
    low = (body or "").lower()
    return [{"kind": "tone", "detail": f'"{p}" reads as passive-aggressive', "severity": "warn"}
            for p in PASSIVE_AGGRESSIVE if p in low]


def max_follow_ups(loop) -> int:
    per = common.cfg("followup").get("escalate_after") or {}
    if loop.get("priority") in per:
        return int(per[loop["priority"]])
    return int((config.load("cadence").get("escalation") or {}).get("max_follow_ups", 2))


def next_after(loop, today: date) -> date:
    """The cadence rule's next look, counted from today: a decision or a nudge restarts the clock."""
    return max(cadence.next_check({**loop, "due_date": None}, today), today + timedelta(days=1))


def _rule_text(loop) -> str:
    for rule in config.load("cadence").get("rules") or []:
        when = rule.get("when") or {}
        if all(loop.get(k) == v for k, v in when.items()):
            return (f"look again {rule.get('check_after_days', 5)} business days after the due date or the call "
                    f"(rule {when or 'default'})")
    return "default cadence"


def _parse_date(raw) -> Optional[date]:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def _clamp(wanted: Optional[date], today: date, cap_days: Optional[int] = None) -> date:
    cap_days = cap_days or int(common.cfg("followup").get("max_wait_days", 30))
    wanted = wanted or cadence.add_business_days(today, 3)
    return min(max(wanted, today + timedelta(days=1)), today + timedelta(days=cap_days))


def owner_addresses(conn, loop) -> set:
    """Who counts as the loop's owner on the buyer side: the linked person, a
    deal person with the owner's name, else everyone buyer-side on the deal."""
    addrs = set()
    if loop.get("owner_person_id"):
        row = conn.execute("SELECT email FROM people WHERE node_id=?", (loop["owner_person_id"],)).fetchone()
        if row and row["email"]:
            addrs.add(row["email"].lower())
    buyers = [p for p in (repo.deal_people(conn, loop["deal_id"]) if loop.get("deal_id") else [])
              if not p["is_me"] and p["email"]]
    name = (loop.get("owner_name") or "").lower().strip()
    if not addrs and name:
        first = name.split()[0]
        addrs = {p["email"].lower() for p in buyers
                 if p["name"].lower() == name or p["name"].lower().split()[0] == first}
    return addrs or {p["email"].lower() for p in buyers}


def _processed(call) -> bool:
    from ..orchestrator import workflow
    state = call["wf_state"]
    if state in workflow.POST_PIPELINE:
        return True
    names = workflow.STEP_NAMES
    return state in names and names.index(state) >= names.index("loops_reconciled")


def gather(conn, loop, today: date) -> dict:
    since = common.ts(loop.get("last_activity_at")) or common.ts(loop.get("created_at"))
    owners = owner_addresses(conn, loop)
    replies, newer_calls = [], []
    if loop.get("deal_id"):
        for r in conn.execute("SELECT * FROM email_replies WHERE deal_id=? ORDER BY received_at", (loop["deal_id"],)):
            received = common.ts(r["received_at"])
            if r["from_addr"].lower() in owners and received and (since is None or received > since):
                replies.append(dict(r))
        for c in conn.execute("SELECT node_id, title, started_at, wf_state, wf_error FROM calls "
                              "WHERE deal_id=? AND node_id IS NOT ? AND wf_state!='live'",
                              (loop["deal_id"], loop.get("call_id"))):
            started = common.ts(c["started_at"])
            if started and since and started > since:
                newer_calls.append(dict(c))
    newer_calls.sort(key=lambda c: common.ts(c["started_at"]))
    nudges = [dict(r) for r in conn.execute(
        "SELECT DISTINCT e.* FROM emails e JOIN followup_decisions f ON f.email_id=e.id "
        "WHERE f.loop_id=? ORDER BY e.id", (loop["node_id"],))]
    sent = [n for n in nudges if n["status"] == "sent" and n["sent_at"]]
    close = conn.execute(
        "SELECT * FROM memory_conflicts WHERE entity_id=? AND field='loops.status' AND status='open' "
        "AND proposed_value IN ('done','cancelled','superseded') ORDER BY id DESC LIMIT 1",
        (loop["node_id"],)).fetchone()
    return {
        "owners": owners, "replies": replies, "newer_calls": newer_calls,
        "pending": [n for n in nudges if n["status"] in PENDING_NUDGE],
        "last_sent": max((common.ts(n["sent_at"]) for n in sent), default=None),
        "close_proposal": dict(close) if close else None,
        "allowed": recipients.allowed_recipients(conn, None, loop.get("deal_id")) if loop.get("deal_id") else {},
        "max": max_follow_ups(loop),
    }


def _facts(loop, f) -> dict:
    return {
        "follow_up_count": loop.get("follow_up_count"), "max_follow_ups": f["max"],
        "due_date": loop.get("due_date"), "next_check_at": loop.get("next_check_at"),
        "last_activity_at": loop.get("last_activity_at"), "deal_status": loop.get("deal_status"),
        "replies_since": [r["id"] for r in f["replies"]], "newer_calls": [c["node_id"] for c in f["newer_calls"]],
        "pending_nudges": [n["id"] for n in f["pending"]],
        "last_nudge_sent": f["last_sent"].isoformat() if f["last_sent"] else None,
        "recipients": sorted(f["allowed"]),
    }


def _rules(conn, loop, f, today: date) -> Optional[Outcome]:
    later = next_after(loop, today)
    tomorrow_b = cadence.add_business_days(today, 1)
    if f["close_proposal"]:
        cp = f["close_proposal"]
        reason = (json.loads(cp["provenance"] or "{}") or {}).get("reason")
        return Outcome("completed", "ask_user",
                       f"There is an open proposal to mark this {cp['proposed_value']}"
                       f"{' (' + reason + ')' if reason else ''}. Settle it before anyone is chased.", tomorrow_b)
    if loop.get("superseded_by"):
        return Outcome("superseded", "skip", f"Replaced by {loop['superseded_by']}; nothing to chase.",
                       today + timedelta(days=30), park=("superseded", "high"))
    if loop.get("deal_status") in ("won", "lost"):
        return Outcome("deal_closed", "close_as_stale",
                       f"The deal is marked {loop['deal_status']}, so this no longer needs chasing. "
                       "Proposed as cancelled for you to confirm.", later, park=("cancelled", "high"))
    if loop.get("deal_status") == "paused":
        return Outcome("deal_paused", "wait_until", "The deal is paused, so this waits two weeks instead of a chase.",
                       wait_until=cadence.add_business_days(today, 10))
    if f["replies"]:
        r = f["replies"][-1]
        return Outcome("replied", "ask_user",
                       f"{r['from_name'] or r['from_addr']} replied on {common.day_label(r['received_at'])} since this "
                       "loop last moved. Read the reply and its proposals before any nudge.", later,
                       activity=r["received_at"])
    if f["newer_calls"]:
        c = f["newer_calls"][-1]
        if not _processed(c):
            return Outcome("newer_call_processing", "wait_until",
                           f"A newer call on {common.day_label(c['started_at'])} is still being processed and may "
                           "settle this.", wait_until=tomorrow_b)
        return Outcome("newer_call", "ask_user",
                       f"You spoke to them on {common.day_label(c['started_at'])} ({c['title'] or 'untitled call'}) "
                       "since this was raised, and that call did not touch it. Confirm it is still open before "
                       "anyone is chased.", later, activity=c["started_at"])
    if loop["owner"] in ("me", "internal"):
        due = f", due {common.day_label(loop['due_date'])}" if loop.get("due_date") else ""
        return Outcome("own_commitment", "ask_user",
                       f"This is your own commitment{due}. There is nothing to send the buyer: do it, re-date it "
                       "or close it.", later)
    if f["pending"]:
        return Outcome("nudge_pending", "ask_user",
                       f"Nudge #{f['pending'][-1]['id']} is drafted and waiting for your Send.", tomorrow_b)
    if loop.get("escalation"):
        return Outcome("already_escalated", "skip", f"Already escalated ({loop['escalation']}); no more nudges.",
                       today + timedelta(days=30))
    if (loop.get("follow_up_count") or 0) >= f["max"]:
        n = loop.get("follow_up_count") or 0
        return Outcome("max_follow_ups", "escalate",
                       f"{n} follow-up{'s' if n != 1 else ''} sent with no response (the limit is {f['max']}). "
                       "Flagged as a deal risk; no more nudges.")
    gap = int(common.cfg("followup").get("min_business_days_between_nudges", 2))
    if f["last_sent"]:
        earliest = cadence.add_business_days(f["last_sent"].astimezone(common.IST).date(), gap)
        if earliest > today:
            return Outcome("too_soon", "wait_until",
                           f"The last nudge went out on {common.day_label(f['last_sent'])}; the next is not due "
                           f"before {common.day_label(earliest)}.", wait_until=earliest)
    if not f["allowed"]:
        return Outcome("no_recipient", "ask_user",
                       "Nobody on this deal has an email address on record, so no nudge can be drafted. "
                       "Add the stakeholder first.", later)
    if today.weekday() >= 5 and not common.cfg("followup").get("nudge_weekends", False):
        return Outcome("weekend", "wait_until", "It is the weekend; follow-ups wait for Monday.",
                       wait_until=today + timedelta(days=7 - today.weekday()))
    return None


# ---- prompts -------------------------------------------------------------------------

def _loop_block(conn, loop, f) -> str:
    call = repo.get_call(conn, loop["call_id"]) if loop.get("call_id") else None
    if call:
        raised = f'call "{call["title"] or "untitled"}" on {common.day_label(call["started_at"])}'
    else:
        raised = "a Jarvis commitment" if loop.get("source") == "world" else "added by hand or from an email"
    owner = loop["owner"] + (f" ({loop['owner_name']})" if loop.get("owner_name") else "")
    lines = [
        f"id: {loop['node_id']}",
        f"what: {loop['description']}",
        f"type: {loop['type']} | owner: {owner} | priority: {loop['priority']} | status: {loop['status']}",
        f"source: {loop['source']} | confidence: {loop['confidence']} | review: {loop['review_state']}",
        f"due: {loop['due_date']} ({loop['due_date_confidence']})" if loop.get("due_date") else "due: none set",
        f"raised: {raised}",
    ]
    if loop.get("evidence_quote"):
        lines.append(f'their words: "{loop["evidence_quote"]}"')
    if loop.get("follow_up_strategy"):
        lines.append(f"follow-up strategy noted when extracted: {loop['follow_up_strategy']}")
    last = common.day_label(f["last_sent"]) if f["last_sent"] else "never"
    lines.append(f"nudges sent so far: {loop.get('follow_up_count') or 0} (escalates at {f['max']}); last: {last}")
    lines.append(f"cadence: {_rule_text(loop)}")
    return "\n".join(lines)


def _deal_block(conn, loop) -> str:
    if not loop.get("deal_id"):
        return "Not linked to a deal."
    people = [p for p in repo.deal_people(conn, loop["deal_id"]) if not p["is_me"]]
    stake = "; ".join(f"{p['name']}{', ' + p['title'] if p['title'] else ''}"
                      f"{' [' + p['role_in_deal'] + ']' if p['role_in_deal'] else ''}" for p in people) or "(none)"
    return (f"{loop.get('deal_name') or loop['deal_id']} | stage {loop.get('deal_stage') or '-'} | status "
            f"{loop.get('deal_status') or '-'} | next step on record: {loop.get('deal_next_step') or '-'}\n"
            f"Stakeholders: {stake}")


def _other_loops(conn, loop) -> str:
    if not loop.get("deal_id"):
        return "(none)"
    rows = conn.execute(
        "SELECT * FROM loops WHERE deal_id=? AND node_id!=? AND status IN ('open','waiting') "
        "AND review_state!='rejected' ORDER BY created_at DESC LIMIT 15", (loop["deal_id"], loop["node_id"])).fetchall()
    return "\n".join(f"- {r['type']} | owner {r['owner']}{' (' + r['owner_name'] + ')' if r['owner_name'] else ''} | "
                     f"due {r['due_date'] or '-'} | {r['status']} | {r['description']}" for r in rows) or "(none)"


def _email_blocks(conn, deal_id, limit=4) -> str:
    if not deal_id:
        return "(none)"
    rows = conn.execute("SELECT kind, subject, body, sent_at, to_addrs FROM emails WHERE deal_id=? AND status='sent' "
                        "ORDER BY sent_at DESC LIMIT ?", (deal_id, limit)).fetchall()
    return "\n".join(
        f'<EMAIL kind="{r["kind"]}" sent="{common.day_label(r["sent_at"])}" '
        f'to="{", ".join(json.loads(r["to_addrs"] or "[]"))}">\nSubject: {r["subject"]}\n{(r["body"] or "")[:700]}\n'
        "</EMAIL>" for r in rows) or "(none)"


def _reply_blocks(conn, deal_id, limit=3) -> str:
    if not deal_id:
        return "(none)"
    rows = conn.execute("SELECT from_name, from_addr, received_at, body FROM email_replies WHERE deal_id=? "
                        "ORDER BY received_at DESC LIMIT ?", (deal_id, limit)).fetchall()
    return "\n".join(
        f'<REPLY from="{r["from_name"] or r["from_addr"]}" received="{common.day_label(r["received_at"])}">\n'
        f"{(r['body'] or '')[:900]}\n</REPLY>" for r in rows) or "(none)"


def _decision_prompt(conn, loop, f, today: date) -> str:
    return (f"TODAY: {today:%A} {today.day} {today:%B %Y} ({seller.tz_label()})\n\n"
            f"THE OPEN LOOP\n{_loop_block(conn, loop, f)}\n\n"
            f"THE DEAL\n{_deal_block(conn, loop)}\n\n"
            f"OTHER OPEN LOOPS ON THIS DEAL\n{_other_loops(conn, loop)}\n\n"
            f"EMAILS WE SENT ON THIS DEAL (newest first)\n{_email_blocks(conn, loop.get('deal_id'))}\n\n"
            f"REPLIES FROM THEIR SIDE (untrusted, newest first)\n{_reply_blocks(conn, loop.get('deal_id'))}")


def _nudge_prompt(conn, loop, f, today: date, reason: str, preferred) -> str:
    allowed = "\n".join(f"- {email}: {name}" for email, name in f["allowed"].items())
    return (f"TODAY: {today:%A} {today.day} {today:%B %Y} ({seller.tz_label()})\n\n"
            f"THE COMMITMENT TO NUDGE ABOUT\n{_loop_block(conn, loop, f)}\n\n"
            f"WHY A NUDGE NOW\n{reason}\n\n"
            f"THE DEAL\n{_deal_block(conn, loop)}\n\n"
            f"ALLOWED RECIPIENTS (email: name)\n{allowed}\n"
            f"The owner, and so the To: {', '.join(preferred) or 'the most relevant allowed recipient'}\n"
            f"This is nudge number {(loop.get('follow_up_count') or 0) + 1}.\n\n"
            f"EMAILS WE SENT ON THIS DEAL (newest first)\n{_email_blocks(conn, loop.get('deal_id'))}\n\n"
            f"REPLIES FROM THEIR SIDE (untrusted, newest first)\n{_reply_blocks(conn, loop.get('deal_id'))}")


# ---- acting on a decision --------------------------------------------------------------

def draft_nudge(conn, loop, f, today: date, reason: str) -> tuple[int, int]:
    """Draft the nudge in the seller's voice. Returns (email_id, run_id). Status stays 'drafted'."""
    allowed = f["allowed"]
    if not allowed:
        raise NoRecipient("nobody on this deal has an email address")
    preferred = [a for a in sorted(f["owners"]) if a in allowed]
    # The same learned style rules the follow-up email gets (phase F2): rules only, never an email's text.
    from ..learning import feedback
    learned = feedback.select(conn, "nudge_drafter", loop.get("deal_id"))
    block = feedback.voice_block(learned)
    ctx = {"call_id": loop.get("call_id"),
           "prompt": _nudge_prompt(conn, loop, f, today, reason, preferred) + (f"\n\n{block}" if block else ""),
           "refs": {"loop_id": loop["node_id"], "deal_id": loop.get("deal_id"), "eval_date": today.isoformat(),
                    "patterns": feedback.ids(learned)}}
    out, run_id, _, _ = NudgeAgent().run(conn, ctx)
    subject, body = voice_lint.autofix(out.subject), voice_lint.autofix(out.body)
    to = [a.lower().strip() for a in out.to if a.lower().strip() in allowed]
    to = list(dict.fromkeys(to)) or preferred[:1] or [next(iter(allowed))]
    cc = [a.lower().strip() for a in out.cc if a.lower().strip() in allowed and a.lower().strip() not in to]
    dropped = recipients.check(out.to, out.cc, allowed)
    issues = ([i.as_dict() for i in voice_lint.lint(subject, body)] + nudge_lint(body)
              + [{"kind": "recipient", "detail": f"dropped from the draft: {d}", "severity": "warn"} for d in dropped
                 if "is not on this call" in d])
    cur = conn.execute(
        "INSERT INTO emails(call_id,deal_id,kind,to_addrs,cc_addrs,subject,body,draft_body,rationale,version,status,"
        "lint,run_id,created_at,updated_at) VALUES (NULL,?,'nudge',?,?,?,?,?,?,1,'drafted',?,?,?,?)",
        (loop.get("deal_id"), json.dumps(to), json.dumps(cc), subject, body, body, out.rationale,
         json.dumps(issues), run_id, now(), now()))
    email_id = cur.lastrowid
    record_items(conn, run_id, created=[f"nudge email {email_id} for {loop['node_id']}"],
                 rejected=[{"recipient": d} for d in dropped])
    bus.publish(conn, Event(type="EMAIL_DRAFT_CREATED", entity_id=loop.get("deal_id"),
                            dedupe_key=f"NUDGE_DRAFT:{email_id}",
                            payload={"email_id": email_id, "loop_id": loop["node_id"], "kind": "nudge"}))
    return email_id, run_id


def _park(conn, loop, status, confidence, decision_id, rationale) -> Optional[int]:
    prov = {"kind": "followup", "ref": f"followup:{decision_id}", "reason": rationale}
    if gate.park(conn, gate.Proposed(loop["node_id"], "loops", "status", status, confidence, prov)) != "needs_review":
        return None
    row = conn.execute("SELECT id FROM memory_conflicts WHERE entity_id=? AND field='loops.status' AND proposed_value=? "
                       "AND status='open' ORDER BY id DESC LIMIT 1", (loop["node_id"], status)).fetchone()
    return row["id"] if row else None


def _escalate(conn, loop, today: date, decision_id: int) -> str:
    """A deal_risk loop for the seller, and the original stops being nudged."""
    lid = loop["node_id"]
    risk_id = "loop-esc-" + hashlib.sha1(lid.encode()).hexdigest()[:12]
    n = loop.get("follow_up_count") or 0
    if not engine.node_exists(conn, risk_id):
        desc = f"No response after {n} follow-up{'s' if n != 1 else ''}: {loop['description']}"
        engine.add_node(conn, ACTOR, id=risk_id, type="loop", kind="deal_risk", title=desc[:200], status="full",
                        confidence=2 / 3, source_id=lid)
        conn.execute(
            "INSERT INTO loops(node_id,deal_id,call_id,type,description,owner,source,confidence,evidence_quote,"
            "evidence_turns,priority,due_date_confidence,status,next_check_at,dependencies,review_state,created_at,"
            "last_activity_at) VALUES (?,?,?,'deal_risk',?,'me','recommended','high',?,?,?,'unknown','open',?,?,"
            "'proposed',?,?)",
            (risk_id, loop.get("deal_id"), loop.get("call_id"), desc, loop.get("evidence_quote"),
             loop.get("evidence_turns") or "[]", "critical" if loop.get("priority") == "critical" else "high",
             today.isoformat(), json.dumps([lid]), now(), now()))
        engine.set_source(conn, ACTOR, risk_id, uri=f"followup:{decision_id}", capture="followup", lineage=[lid])
        gate.set_initial(conn, risk_id, "loops", "status", "open", "high", {"kind": "followup", "ref": lid})
    conn.execute("UPDATE loops SET escalation=? WHERE node_id=?", (f"flag_deal_risk:{risk_id}", lid))
    return risk_id


def _apply(conn, loop, today: date, stage: str, o: Outcome, facts: dict, f, publish: bool = True) -> dict:
    lid = loop["node_id"]
    if o.decision == "wait_until":
        o.wait_until = _clamp(o.wait_until, today, 365 if stage == USER_STAGE else None)
        o.next_check = o.wait_until
    elif o.decision == "escalate":
        o.next_check = None
    elif o.next_check is None:
        o.next_check = next_after(loop, today)
    run_id = o.run_id or common.record_rules_run(
        conn, ACTOR, {"loop_id": lid, "eval_date": today.isoformat(), "stage": stage},
        {"check": o.check, "decision": o.decision, "rationale": o.rationale, "facts": facts},
        call_id=loop.get("call_id"))
    cur = conn.execute(
        "INSERT INTO followup_decisions(loop_id,deal_id,eval_date,stage,check_name,decision,rationale,"
        "relationship_risk,relationship_note,wait_until,next_check_at,facts,run_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lid, loop.get("deal_id"), today.isoformat(), stage, o.check, o.decision, o.rationale, o.risk, o.note,
         o.wait_until.isoformat() if o.wait_until else None, o.next_check.isoformat() if o.next_check else None,
         json.dumps(facts, default=str), run_id, now()))
    did = cur.lastrowid
    result = {"decision_id": did, "loop_id": lid, "stage": stage, "check": o.check, "decision": o.decision,
              "rationale": o.rationale}
    if o.decision == "send_nudge":
        try:
            email_id, nudge_run = draft_nudge(conn, loop, f, today, o.rationale)
            conn.execute("UPDATE followup_decisions SET email_id=?, nudge_run_id=? WHERE id=?", (email_id, nudge_run, did))
            result["email_id"] = email_id
        except (AgentFailed, NoRecipient) as exc:
            o.decision = "ask_user"
            o.rationale = f"{o.rationale} Drafting the nudge failed, so nothing was drafted: {exc}"[:2000]
            conn.execute("UPDATE followup_decisions SET decision='ask_user', rationale=? WHERE id=?",
                         (o.rationale, did))
            result.update(decision="ask_user", rationale=o.rationale)
    elif o.decision == "escalate":
        risk_id = _escalate(conn, loop, today, did)
        conn.execute("UPDATE followup_decisions SET risk_loop_id=? WHERE id=?", (risk_id, did))
        result["risk_loop_id"] = risk_id
    park = o.park or (("cancelled", "medium") if o.decision == "close_as_stale" else None)
    if park:
        conflict_id = _park(conn, loop, park[0], park[1], did, o.rationale)
        if conflict_id:
            conn.execute("UPDATE followup_decisions SET conflict_id=? WHERE id=?", (conflict_id, did))
            result["conflict_id"] = conflict_id
    if o.activity:
        conn.execute("UPDATE loops SET last_activity_at=? WHERE node_id=?", (o.activity, lid))
    nxt = o.next_check.isoformat() if o.next_check else None
    conn.execute("UPDATE loops SET next_check_at=? WHERE node_id=?", (nxt, lid))
    engine._emit(conn, ACTOR, "followup_decided", node_id=lid,
                 after={"decision_id": did, "stage": stage, "check": o.check, "decision": o.decision,
                        "next_check_at": nxt})
    if publish:
        bus.publish(conn, Event(type="FOLLOW_UP_DUE", entity_id=lid, dedupe_key=f"FOLLOW_UP_DUE:{lid}:{today.isoformat()}",
                                payload={"loop_id": lid, "decision_id": did, "decision": o.decision, "check": o.check}))
    result["next_check_at"] = nxt
    return result


# ---- entry points ------------------------------------------------------------------------

def evaluate_loop(conn, loop: dict, today: date, force_nudge: bool = False) -> dict:
    f = gather(conn, loop, today)
    facts = _facts(loop, f)
    rule = _rules(conn, loop, f, today)
    if force_nudge:
        if rule and rule.check in ("completed", "own_commitment", "nudge_pending", "no_recipient"):
            return _apply(conn, loop, today, USER_STAGE, rule, facts, f, publish=False)
        return _apply(conn, loop, today, USER_STAGE, Outcome("requested", "send_nudge", "You asked for a nudge on this loop."),
                      facts, f, publish=False)
    if rule is not None:
        return _apply(conn, loop, today, "rules", rule, facts, f)
    ctx = {"call_id": loop.get("call_id"), "prompt": _decision_prompt(conn, loop, f, today),
           "refs": {"loop_id": loop["node_id"], "deal_id": loop.get("deal_id"), "eval_date": today.isoformat()}}
    try:
        out, run_id, _, _ = FollowupAgent().run(conn, ctx)
    except AgentFailed as exc:
        return _apply(conn, loop, today, "rules",
                      Outcome("agent_failed", "ask_user",
                              f"The follow-up agent failed, so nothing was decided automatically: {exc}"[:800],
                              today + timedelta(days=1)), facts, f)
    o = Outcome("agent", out.decision, out.rationale, wait_until=_parse_date(out.wait_until),
                risk=out.relationship_risk, note=out.relationship_note, run_id=run_id)
    if o.decision == "send_nudge" and (out.relationship_risk == "high" or not out.still_relevant):
        why = "relationship risk is high" if out.relationship_risk == "high" else "it may no longer be relevant"
        o.decision = "ask_user"
        o.rationale = f"{o.rationale} A nudge was suggested, but {why}, so it waits for your call."
    return _apply(conn, loop, today, "agent", o, facts, f)


def schedule_unscheduled(conn, today: date) -> int:
    """Tracked loops with no next_check_at (hand-added, Jarvis imports) get one from the cadence rules."""
    count = 0
    for r in conn.execute(f"SELECT l.* FROM loops l WHERE l.status IN ('open','waiting') AND {TRACKED} "
                          "AND l.next_check_at IS NULL AND l.escalation IS NULL").fetchall():
        loop = dict(r)
        try:
            nxt = cadence.next_check(loop, common.ist_date(loop["created_at"]) or today)
        except ValueError:
            nxt = today
        conn.execute("UPDATE loops SET next_check_at=? WHERE node_id=?", (nxt.isoformat(), loop["node_id"]))
        engine._emit(conn, ACTOR, "loop_check_scheduled", node_id=loop["node_id"], after={"next_check_at": nxt.isoformat()})
        count += 1
    return count


def due_loops(conn, today: date) -> list:
    return conn.execute(
        LOOP_SELECT + f"WHERE l.status IN ('open','waiting') AND {TRACKED} AND l.next_check_at IS NOT NULL "
        f"AND substr(l.next_check_at,1,10)<=? ORDER BY {PRIORITY_SQL}, l.next_check_at", (today.isoformat(),)).fetchall()


def evaluate_due(conn, today: Optional[date] = None) -> list[dict]:
    """Decide every due loop once per day. Safe to call repeatedly."""
    today = today or common.today_ist()
    schedule_unscheduled(conn, today)
    conn.commit()
    decided = {r["loop_id"] for r in conn.execute(
        "SELECT loop_id FROM followup_decisions WHERE eval_date=? AND stage IN ('rules','agent')", (today.isoformat(),))}
    results = []
    for row in due_loops(conn, today):
        if row["node_id"] in decided:
            continue
        results.append(evaluate_loop(conn, dict(row), today))
        conn.commit()
    set_state(conn, "automation:followups:last_eval",
              json.dumps({"date": today.isoformat(), "at": now(), "decisions": len(results)}))
    conn.commit()
    return results


def _loop(conn, loop_id):
    row = conn.execute(LOOP_SELECT + "WHERE l.node_id=?", (loop_id,)).fetchone()
    if row is None:
        raise KeyError(f"loop {loop_id}")
    return dict(row)


def evaluate_one(conn, loop_id: str, today: Optional[date] = None, force_nudge: bool = False) -> dict:
    result = evaluate_loop(conn, _loop(conn, loop_id), today or common.today_ist(), force_nudge=force_nudge)
    conn.commit()
    return result


def snooze(conn, loop_id: str, until: date, today: Optional[date] = None) -> dict:
    today = today or common.today_ist()
    result = _apply(conn, _loop(conn, loop_id), today, USER_STAGE,
                    Outcome("snoozed", "wait_until", f"You asked to look again on {common.day_label(until)}.",
                            wait_until=until), {}, None, publish=False)
    conn.commit()
    return result


def still_open(conn, loop_id: str, today: Optional[date] = None) -> dict:
    today = today or common.today_ist()
    result = _apply(conn, _loop(conn, loop_id), today, USER_STAGE,
                    Outcome("still_open", "skip", "You confirmed it is still open; it is evaluated again on the next run.",
                            next_check=today, activity=now()), {}, None, publish=False)
    conn.commit()
    return result


def on_email_sent(conn, event) -> int:
    """EMAIL_SENT handler: a nudge that really went out counts, exactly once."""
    email_id = (event.payload or {}).get("email_id")
    row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if row is None or row["kind"] != "nudge" or row["status"] != "sent":
        return 0
    today, counted = common.today_ist(), 0
    for d in conn.execute("SELECT * FROM followup_decisions WHERE email_id=? AND sent_counted_at IS NULL",
                          (email_id,)).fetchall():
        loop = conn.execute("SELECT * FROM loops WHERE node_id=?", (d["loop_id"],)).fetchone()
        if loop is None:
            continue
        nxt = next_after(dict(loop), today).isoformat()
        conn.execute("UPDATE loops SET follow_up_count=follow_up_count+1, last_activity_at=?, next_check_at=? "
                     "WHERE node_id=?", (now(), nxt, d["loop_id"]))
        conn.execute("UPDATE followup_decisions SET sent_counted_at=? WHERE id=?", (now(), d["id"]))
        engine._emit(conn, ACTOR, "nudge_sent", node_id=d["loop_id"],
                     after={"email_id": email_id, "follow_up_count": loop["follow_up_count"] + 1,
                            "approved_by": row["approved_by"], "next_check_at": nxt})
        counted += 1
    conn.commit()
    return counted


def why(conn, email_id: int) -> dict:
    """Everything behind one nudge: the loop, each decision and its run, who approved it, auto-send checks."""
    email = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if email is None:
        raise KeyError(f"email {email_id}")
    decisions = [dict(r) for r in conn.execute("SELECT * FROM followup_decisions WHERE email_id=? ORDER BY id",
                                               (email_id,))]
    loop_ids = list(dict.fromkeys(d["loop_id"] for d in decisions))
    loops = [dict(r) for r in conn.execute(
        f"SELECT * FROM loops WHERE node_id IN ({','.join('?' * len(loop_ids))})", loop_ids)] if loop_ids else []
    history = [dict(r) for r in conn.execute(
        f"SELECT * FROM followup_decisions WHERE loop_id IN ({','.join('?' * len(loop_ids))}) ORDER BY id",
        loop_ids)] if loop_ids else []
    autosend = [dict(r) for r in conn.execute("SELECT * FROM autosend_log WHERE email_id=? ORDER BY id", (email_id,))]
    return {"email": dict(email), "decisions": decisions, "loops": loops, "history": history, "autosend": autosend,
            "approved_by": email["approved_by"], "sent_at": email["sent_at"]}


def explain(info: dict) -> str:
    """why() as plain text, for the CLI."""
    e = info["email"]
    lines = [f"Email {e['id']} ({e['kind']}, {e['status']}) to {', '.join(json.loads(e['to_addrs'] or '[]'))}: "
             f"{e['subject']}"]
    for loop in info["loops"]:
        lines.append(f"Loop {loop['node_id']}: {loop['description']} (owner {loop['owner']}, due "
                     f"{loop['due_date'] or '-'}, {loop['follow_up_count']} nudges sent)")
        if loop.get("evidence_quote"):
            lines.append(f'  their words: "{loop["evidence_quote"]}"')
    for d in info["history"]:
        mark = "  <- drafted this email" if d["email_id"] == e["id"] else ""
        lines.append(f"  {d['eval_date']} [{seller.display(d['stage'])}/{d['check_name']}] "
                     f"{seller.display(d['decision'])}: {d['rationale']}"
                     f" (run {d['run_id']}){mark}")
    if e["status"] == "sent":
        lines.append(f"Sent {e['sent_at']} because {seller.display(e['approved_by']) or 'unknown'} approved it.")
    else:
        lines.append("Not sent. It waits for your Send.")
    for a in info["autosend"]:
        lines.append(f"  auto-send {a['created_at']}: {a['outcome']} {json.loads(a['reasons'] or '[]')}")
    return "\n".join(lines)
