"""Auto-send executor: the only code that may send mail without a click on Send.

It exists because spec §9 names two auto-send policies, and it is built to
refuse. An email goes out only when EVERY one of these holds, checked fresh on
each run:
  1. config/policy.yaml email_policy is LOW_RISK_FOLLOWUPS_AUTO_SEND or
     APPROVED_CONTACTS_AUTO_SEND (the seller edits that file by hand);
  2. config/automation.yaml auto_send.enabled is true and dry_run is false;
  3. policy.evaluate finds no blocking issue;
  4. APPROVED_CONTACTS: every recipient is in policy.yaml approved_contacts.
     LOW_RISK: a nudge or follow-up, one recipient, lint clean, no pricing or
     commercial terms, no numbers beyond dates and times, no commitment that
     is not a confirmed loop, and not the first email to that person;
  5. the draft has sat for hold_minutes, today's cap is not used up, and it is
     a weekday between 09:30 and 19:00 IST.
Then it calls policy.approve_and_send(approved_by="policy:<name>"), so the send
is still exactly-once and lint-gated. In dry run it records "would auto-send
because ..." and sends nothing. It ships disabled.
"""
import difflib
import functools
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import config, seller
from ..execution import policy
from ..store.stores import now
from ..validators import evidence, gates
from . import common
from .followup import nudge_lint

KINDS = ("nudge", "followup")


def _auto_cfg() -> dict:
    """automation.yaml as it is now (config.load re-reads an edited file), so enabled: false stops auto-send at once."""
    return config.load("automation").get("auto_send") or {}
COMMERCIAL = re.compile(
    r"(₹|\$|€|£|\brs\.?\s*\d|\binr\b|\busd\b|\blakhs?\b|\bcrores?\b|\bpric(e|es|ing)\b|\bcosts?\b|\bdiscounts?\b|"
    r"\binvoices?\b|\bquot(e|es|ation)\b|\bcontracts?\b|\bsow\b|\bpayments?\b|\bfees?\b|\brate card\b|\bbudgets?\b|"
    r"\bterms\b|\bpurchase order\b|\bcommercials?\b|\bproposals?\b|\bper (month|year|shipment|truck|trip|km)\b)",
    re.I)
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DATE_TIME = re.compile(
    rf"\b\d{{1,2}}[:.]\d{{2}}\b|\b\d{{1,2}}\s?(am|pm)\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b|"
    rf"\b\d{{1,2}}(st|nd|rd|th)?\s+{_MONTH}\b|\b{_MONTH}\s+\d{{1,2}}(st|nd|rd|th)?\b", re.I)
COMMITMENT = re.compile(r"\b(i'll|i will|we'll|we will|i can|we can|i'd be happy to|i promise|we promise|"
                        r"guarantee[sd]?|commit(ted)? to)\b", re.I)


@dataclass
class Verdict:
    ok: bool
    policy: str
    dry_run: bool
    fails: list = field(default_factory=list)
    because: list = field(default_factory=list)


def _confirmed_loops(conn, row) -> list[str]:
    if row["kind"] == "nudge":
        rows = conn.execute(
            "SELECT l.description FROM followup_decisions f JOIN loops l ON l.node_id=f.loop_id WHERE f.email_id=? "
            "AND (l.review_state='confirmed' OR l.source='world')", (row["id"],)).fetchall()
    else:
        rows = conn.execute("SELECT description FROM loops WHERE call_id=? AND review_state='confirmed' "
                            "AND status!='cancelled'", (row["call_id"],)).fetchall()
    return [r[0] for r in rows]


def _covered(sentence: str, loops: list[str]) -> bool:
    s = evidence.normalize(sentence)
    words = set(s.split())
    for loop in loops:
        content = [w for w in evidence.normalize(loop).split() if len(w) > 3]
        if content and sum(w in words for w in content) / len(content) >= 0.5:
            return True
        if difflib.SequenceMatcher(None, s, evidence.normalize(loop)).ratio() >= 0.6:
            return True
    return False


def _known_contact(conn, addr: str, email_id: int) -> bool:
    like = f'%"{addr}"%'
    if conn.execute("SELECT 1 FROM emails WHERE status='sent' AND id!=? AND (lower(to_addrs) LIKE ? "
                    "OR lower(cc_addrs) LIKE ?) LIMIT 1", (email_id, like, like)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM email_replies WHERE from_addr=? LIMIT 1", (addr,)).fetchone() is not None


def low_risk_problems(conn, row, issues) -> list[str]:
    """Why an email is NOT low-risk. Empty means it qualifies."""
    problems = []
    to = [a.lower() for a in json.loads(row["to_addrs"] or "[]")]
    cc = json.loads(row["cc_addrs"] or "[]")
    body = row["body"] or ""
    if row["kind"] not in KINDS:
        problems.append(f"kind {row['kind']} is not a nudge or follow-up")
    if len(to) != 1 or cc:
        problems.append("more than one recipient")
    lint = list(issues) + nudge_lint(body)
    if lint:
        problems.append("lint is not clean: " + "; ".join(i["detail"] for i in lint)[:300])
    hit = COMMERCIAL.search(f"{row['subject'] or ''}\n{body}")
    if hit:
        problems.append(f'mentions commercial terms ("{hit.group(0)}")')
    if re.search(r"\d", DATE_TIME.sub(" ", f"{row['subject'] or ''}\n{body}")):
        problems.append("contains a number other than a date or time")
    loops = _confirmed_loops(conn, row)
    if row["kind"] == "followup" and conn.execute(
            "SELECT 1 FROM loops WHERE call_id=? AND review_state='proposed' LIMIT 1", (row["call_id"],)).fetchone():
        problems.append("the call still has unconfirmed loops")
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
        if COMMITMENT.search(sentence) and not _covered(sentence, loops):
            problems.append(f'makes a commitment that is not a confirmed loop: "{sentence.strip()[:120]}"')
            break
    for addr in to:
        if not _known_contact(conn, addr, row["id"]):
            problems.append(f"first email to {addr}")
    return problems


def _sent_today(conn, today) -> int:
    rows = conn.execute("SELECT created_at FROM autosend_log WHERE outcome='sent' ORDER BY id DESC LIMIT 200").fetchall()
    return sum(1 for r in rows if common.ist_date(r["created_at"]) == today)


def check(conn, row, now_dt: datetime | None = None) -> Verdict:
    auto = _auto_cfg()
    current = (now_dt or common.now_ist()).astimezone(common.IST)
    pol = policy.current_policy()
    v = Verdict(ok=False, policy=pol, dry_run=auto.get("dry_run") is not False)
    if pol in policy.AUTO_POLICIES:
        v.because.append(f"config/policy.yaml names {pol}")
    else:
        v.fails.append(f"email_policy is {pol}; nothing auto-sends under it")
    if auto.get("enabled") is True:
        v.because.append("auto_send is enabled")
    else:
        v.fails.append("auto_send.enabled is false in config/automation.yaml")
    if row["status"] != "drafted":
        v.fails.append(f"the email is {row['status']}, not a draft")
    if row["kind"] == "nudge":
        behind = conn.execute("SELECT l.node_id, l.confidence, l.review_state, l.source FROM followup_decisions f "
                              "JOIN loops l ON l.node_id=f.loop_id WHERE f.email_id=?", (row["id"],)).fetchall()
        weak = [r["node_id"] for r in behind
                if not gates.can_feed_external(r["confidence"], r["review_state"], r["source"])]
        if not behind:
            v.fails.append("not linked to a follow-up decision")
        elif weak:
            v.fails.append("the commitment behind it is too weak for an external action: " + ", ".join(weak))
    decision = policy.evaluate(conn, row)
    blocks = [i["detail"] for i in decision.issues if i["severity"] == "block"]
    if blocks:
        v.fails.append("blocked: " + "; ".join(blocks))
    else:
        v.because.append("the policy check found nothing blocking")
    to = [a.lower() for a in json.loads(row["to_addrs"] or "[]")]
    cc = [a.lower() for a in json.loads(row["cc_addrs"] or "[]")]
    if pol == "APPROVED_CONTACTS_AUTO_SEND":
        approved = {a.lower().strip() for a in (config.load("policy").get("approved_contacts") or [])}
        outside = [a for a in to + cc if a not in approved]
        if outside:
            v.fails.append("not on approved_contacts: " + ", ".join(outside))
        else:
            v.because.append("every recipient is on approved_contacts")
    elif pol == "LOW_RISK_FOLLOWUPS_AUTO_SEND":
        problems = low_risk_problems(conn, row, decision.issues)
        v.fails += problems
        if not problems:
            v.because.append(f"low risk: a {row['kind']} to one known contact, lint clean, no commercial terms, "
                             "numbers or new commitments")
    hold = timedelta(minutes=int(auto.get("hold_minutes", 60)))
    written = common.ts(row["updated_at"] or row["created_at"])
    if written and written + hold > current:
        v.fails.append(f"the draft is younger than {int(hold.total_seconds() // 60)} minutes")
    quiet = auto.get("quiet_hours") or {}
    start, end = common.hhmm(quiet.get("start", "09:30")), common.hhmm(quiet.get("end", "19:00"))
    if current.weekday() >= 5 or not (start <= current.time() < end):
        v.fails.append(f"outside sending hours (weekdays {start:%H:%M} to {end:%H:%M} {seller.tz_label()})")
    else:
        v.because.append("inside sending hours")
    cap = int(auto.get("daily_cap", 3))
    if _sent_today(conn, current.date()) >= cap:
        v.fails.append(f"today's cap of {cap} auto-sends is used up")
    v.ok = not v.fails
    return v


def _log(conn, email_id, pol, outcome, reasons, dry_run, always=False, at=None):
    last = conn.execute("SELECT outcome, reasons FROM autosend_log WHERE email_id=? ORDER BY id DESC LIMIT 1",
                        (email_id,)).fetchone()
    body = json.dumps(reasons)
    if not always and last is not None and (last["outcome"], last["reasons"]) == (outcome, body):
        return
    conn.execute("INSERT INTO autosend_log(email_id,policy,outcome,reasons,dry_run,created_at) VALUES (?,?,?,?,?,?)",
                 (email_id, pol, outcome, body, int(dry_run), at or now()))


def run_once(conn, gmail_factory=None, now_dt: datetime | None = None) -> dict:
    """One pass over drafted nudges and follow-ups. A no-op unless auto-send is enabled."""
    # Every log row carries the evaluation time, so the daily cap counts the same clock it checks.
    now_dt = (now_dt or common.now_ist()).astimezone(common.IST)
    log = functools.partial(_log, at=now_dt.astimezone(timezone.utc).isoformat(timespec="seconds"))
    auto = _auto_cfg()
    if auto.get("enabled") is not True:
        return {"skipped": "auto_send.enabled is false"}
    pol = policy.current_policy()
    if pol not in policy.AUTO_POLICIES:
        return {"skipped": f"email_policy is {pol}"}
    outcomes, gmail = [], None
    rows = conn.execute("SELECT * FROM emails WHERE status='drafted' AND kind IN ('nudge','followup') ORDER BY id").fetchall()
    for row in rows:
        v = check(conn, row, now_dt)
        if not v.ok:
            log(conn, row["id"], v.policy, "refused", v.fails, v.dry_run)
            outcomes.append({"email_id": row["id"], "outcome": "refused", "reasons": v.fails})
            continue
        if v.dry_run:
            reasons = ["would auto-send because " + "; ".join(v.because)]
            log(conn, row["id"], v.policy, "would_send", reasons, True)
            outcomes.append({"email_id": row["id"], "outcome": "would_send", "reasons": reasons})
            continue
        # approve_and_send re-reads the row under an IMMEDIATE lock: a row another path moved to
        # 'sending' or 'sent' since check() is refused or reported as a duplicate, never sent twice.
        # A 'sending' email (delivery unknown) is never picked up again: only 'drafted' rows are read.
        try:
            gmail = gmail or gmail_factory()
            result = policy.approve_and_send(conn, row["id"], gmail, mode="send", approved_by=f"policy:{v.policy}")
        except policy.SendRefused as exc:
            log(conn, row["id"], v.policy, "refused", [f"send refused: {exc}"[:500]], False, always=True)
            conn.commit()
            outcomes.append({"email_id": row["id"], "outcome": "refused", "reasons": [str(exc)]})
            continue
        except Exception as exc:
            log(conn, row["id"], v.policy, "failed", [f"{type(exc).__name__}: {exc}"[:500]], False, always=True)
            conn.commit()
            outcomes.append({"email_id": row["id"], "outcome": "failed", "reasons": [str(exc)]})
            continue
        if result.get("duplicate"):
            outcomes.append({"email_id": row["id"], "outcome": "refused", "reasons": ["already sent; not sent again"]})
            continue
        log(conn, row["id"], v.policy, "sent", ["sent because " + "; ".join(v.because)], False, always=True)
        conn.commit()
        if row["kind"] == "followup" and row["call_id"]:
            from ..orchestrator import review
            try:
                review.email_done(conn, row["call_id"], sent=True)
            except ValueError:
                pass                      # another email on the call is unresolved; the call stays open
        outcomes.append({"email_id": row["id"], "outcome": "sent", "reasons": v.because})
        conn.commit()
    conn.commit()
    return {"policy": pol, "dry_run": auto.get("dry_run") is not False, "emails": outcomes}


def status(conn) -> dict:
    auto = _auto_cfg()
    pol = policy.current_policy()
    armed = pol in policy.AUTO_POLICIES and auto.get("enabled") is True and auto.get("dry_run") is False
    recent = [dict(r) for r in conn.execute("SELECT * FROM autosend_log ORDER BY id DESC LIMIT 20")]
    return {"email_policy": pol, "enabled": auto.get("enabled") is True, "dry_run": auto.get("dry_run") is not False,
            "armed": armed, "daily_cap": int(auto.get("daily_cap", 3)),
            "sent_today": _sent_today(conn, common.today_ist()), "recent": recent}
