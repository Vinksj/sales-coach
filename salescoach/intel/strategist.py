"""The Deal Strategist: from a pile of calls to one next move.

It reads the whole deal (intel/history.py) and returns a stakeholder map,
the state of every element of the active sales methodology (intel/methodology.py:
MEDDPICC unless the org chose another), risks, four assessments, ONE next best
action and a health score. This module decides what of that is stored:

  * every quote is checked against the turns of the call it cites
    (validators/evidence.judge); a fabricated quote drops the item to low;
  * people: a listed id, a known email or name, or one of the seller's contact
    files. A brand-new person is created only when a verified quote names them
    (or their role, e.g. "the CFO"); the seller is never a stakeholder;
  * an element is "known" only with verified evidence, "partial" needs some;
  * an element whose methodology says so raises its risk while it is unknown;
  * a single-threaded deal gets its risk from the data even if the model
    misses it;
  * the health score is capped by facts (false deal optimism guard): no
    dated buyer commitment, one buyer-side voice, a critical element of the
    methodology not known (MEDDPICC: the economic buyer), too few elements
    known. Pleasant calls do not raise the ceiling.

Writes go through the memory gate (intel/tables.py). The strategist's
assessments sit next to the analyst's in `assessments`, never replacing them,
and intel/reconcile.py records a verdict where they disagree.

The pipeline step never blocks the follow-up email: if the strategist fails,
the failure is stored as a 'strategy' artifact the deal page shows with a
retry button, and the pipeline moves on.
"""
import json
import logging
import re
from datetime import date

from .. import repo, seller
from ..agents.base import AgentFailed, record_items
from ..memory import gate
from ..orchestrator import context
from ..schemas.common import CONFIDENCE_RANK, conf_min
from ..store.stores import engine, now
from ..store import db
from ..validators import evidence as ev
from ..validators import voice_lint
from . import history, methodology, reconcile, tables
from .agentkit import PROMPTS, IntelAgent, shas
from .schemas import SUBJECTS, DealStrategy, strategy_model

log = logging.getLogger("salescoach.intel")
ACTOR = tables.ACTOR
BUYER_OWNERS = ("prospect", "mutual")
SEVERITY_ORDER = tables.SEVERITY_ORDER


class StrategistAgent(IntelAgent):
    """One agent per run: the methodology is read once, so the prompt that names the elements and
    the schema that enforces them cannot disagree, even if the setting changes mid-run."""
    name = "deal_strategist"
    schema = DealStrategy                      # the default (MEDDPICC) contract; an instance carries its own

    def __init__(self, m=None):
        self.methodology = m or methodology.active()
        self.schema = strategy_model(self.methodology.keys)

    def system_prompt(self, ctx):
        return methodology.render_prompt(PROMPTS / f"{self.name}.md", self.methodology)

    def build_prompt(self, ctx):
        return history.render(ctx, self.methodology)

    def input_refs(self, ctx):
        # Which methodology a run scored the deal against: the first run after a switch creates rows for
        # elements that are new to the TABLE, not newly learned on the call, and a reader must be able to tell.
        # "patterns": the learned priors this run was shown (phase F2), so their effect can be measured later.
        return {**super().input_refs(ctx), "methodology": self.methodology.key,
                "patterns": [p["id"] for p in ctx.get("learned_priors") or []]}


# ---- evidence ----------------------------------------------------------------------

def judge_entries(entries, stated, ctx, requires_quote=True):
    """Check each cited quote in its own call. Returns (confidence, notes, annotated entries).

    Strict on purpose: one fabricated quote among several drops the whole item to low."""
    if not entries:
        return ("low", ["no evidence cited"], []) if requires_quote else (stated, [], [])
    conf, notes, out = stated, [], []
    for e in entries:
        e = dict(e)
        turns = ctx["turns_by_call"].get(e.get("call_id"))
        if turns is None:
            e["found"], e["notes"] = False, [f"{e.get('call_id')} is not a call of this deal"]
            conf = "low"
        else:
            c, n, check = ev.judge(stated, e.get("quote", ""), e.get("turns", []), turns, requires_quote=requires_quote)
            e["found"], e["notes"] = check.found, n
            conf = conf_min(conf, c)
        notes += e["notes"]
        out.append(e)
    return conf, notes, out


def _cited_text(entries, ctx) -> str:
    words = []
    for e in entries:
        if not e.get("found"):
            continue
        turns = ctx["turns_by_call"].get(e["call_id"]) or {}
        for i in e.get("turns", []):
            for j in (i - 1, i, i + 1):
                if j in turns:
                    words.append(turns[j]["text"])
    return ev.normalize(" ".join(words))


# ---- people --------------------------------------------------------------------------

def _is_me(ctx, name) -> bool:
    """The whole name, not the first name: a buyer who shares the seller's first name is not the seller."""
    me = ctx.get("me") or {}
    n = ev.normalize(name)
    mine = ev.normalize(me.get("name") or seller.name())
    return bool(n) and n == mine


def _resolve_person(conn, s, ctx):
    known = ctx["known_people"]
    contacts = ctx["contacts"]
    pid = (s.get("person_id") or "").strip() or None
    if pid in known:
        return pid, {"how": "known"}
    if pid and pid.startswith("contact:"):
        email = pid.split(":", 1)[1].lower()
        c = next((c for c in contacts if c["email"] == email), None)
        if c:
            return None, {"how": "contact", "contact": c}
    email = (s.get("email") or "").lower().strip()
    if email:
        row = conn.execute("SELECT node_id, is_me FROM people WHERE email=?", (email,)).fetchone()
        if row:
            return (None, {"how": "reject", "why": f"that is {seller.first_name() or 'the seller'}"}) if row["is_me"] else (row["node_id"], {"how": "known"})
        c = next((c for c in contacts if c["email"] == email), None)
        if c:
            return None, {"how": "contact", "contact": c}
    name = (s.get("name") or "").strip()
    if name:
        if _is_me(ctx, name):
            return None, {"how": "reject", "why": f"that is {seller.first_name() or 'the seller'}"}
        low = name.lower()
        match = [p for p in known.values() if (p.get("name") or "").lower() == low]
        if not match:
            # Same name at another account is another person; only this deal's account is searched.
            match = [dict(r) for r in conn.execute(
                "SELECT p.* FROM people p JOIN deals d ON d.account_id=p.account_id "
                "WHERE d.node_id=? AND lower(p.name)=? AND p.is_me=0", (ctx["deal_id"], low))]
        if len(match) == 1:
            return match[0]["node_id"], {"how": "known"}
        c = next((c for c in contacts if c["name"].lower() == low), None)
        if c:
            return None, {"how": "contact", "contact": c}
    if pid:
        return None, {"how": "reject", "why": f"unknown person id {pid}"}
    return None, {"how": "new"}


def _role_title(role: str) -> str:
    return (role or "").split(",")[0].strip()[:60] or "Stakeholder"


def _new_person_named(s, entries, ctx) -> str | None:
    """Why a proposed new person may NOT be created, or None if a verified quote names them."""
    if not any(e.get("found") for e in entries):
        return "a new person needs a verified quote from a call"
    hay = set(_cited_text(entries, ctx).split())
    if s.get("name"):
        tokens = [t for t in ev.normalize(s["name"]).split() if len(t) >= 3]
    else:
        tokens = [t for t in ev.normalize(_role_title(s.get("role"))).split() if len(t) >= 2]
    if not tokens or not any(t in hay for t in tokens):
        return "not named in the cited turns"
    return None


def _create_person(conn, deal_id, account_id, s, call_id) -> str:
    contact = s.get("contact") or {}
    name = s.get("name") or contact.get("name") or f"{_role_title(s.get('role'))} (name unknown)"
    existing = conn.execute(
        "SELECT p.node_id FROM people p LEFT JOIN deal_people dp ON dp.person_id=p.node_id AND dp.deal_id=? "
        "WHERE lower(p.name)=lower(?) AND p.is_me=0 AND (dp.deal_id IS NOT NULL OR p.account_id IS ?) LIMIT 1",
        (deal_id, name, account_id)).fetchone()
    if existing:
        return existing["node_id"]
    email = (s.get("email") or contact.get("email") or "").lower().strip() or None
    if email and repo.find_person_by_email(conn, email):
        return repo.find_person_by_email(conn, email)
    return repo.create_person(conn, name, email=email, account_id=account_id, title=_role_title(s.get("role")),
                              contact_file=contact.get("path"), actor=ACTOR, source_id=call_id)


# ---- validation ----------------------------------------------------------------------

def _buyer_side(ctx) -> list[str]:
    ids = set()
    for c in ctx["calls"]:
        ids |= {p["node_id"] for p in c["participants"] if not p["is_me"]}
    for turns in ctx["turns_by_call"].values():
        ids |= {t["person_id"] for t in turns.values() if t["channel"] == "them" and t.get("person_id")}
    names = {p["node_id"]: p["name"] for p in ctx["known_people"].values()}
    return sorted(names.get(i, i) for i in ids)


def _fix(text):
    return voice_lint.autofix(text or "")


_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                            "nov", "dec"))}
_WEEKDAY_DATE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+(?:(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"([a-z]{3})[a-z]*|([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?)\b", re.I)


def weekday_mismatches(text: str, today: str) -> list[str]:
    """Moved to validators/dates.py so the email lint uses the same check."""
    from ..validators.dates import weekday_mismatches as check
    return check(text, today)


def _band(score: int) -> str:
    for low, label in history.cfg().get("health", {}).get("bands") or [[85, "strong"], [70, "healthy"], [50, "fair"],
                                                                        [25, "weak"], [0, "at risk"]]:
        if score >= low:
            return label
    return "at risk"


def health_caps(ctx, meddpicc, buyers, m=None) -> list[dict]:
    """Facts that put a ceiling on deal health. Two are about the deal whatever the methodology
    (one buyer-side voice, no dated buyer commitment) and need their rule in intel.yaml. The rest come
    from the methodology: each critical element that is not known, and too few elements known. For
    those the number in intel.yaml health.caps[<rule>] wins when there is one (the install's own
    tuning), else the methodology's."""
    m = m or methodology.active()
    caps = (history.cfg().get("health") or {}).get("caps") or {}
    status = {x["element"]: x["status"] for x in meddpicc}
    out = []
    if len(buyers) <= 1 and "single_threaded" in caps:
        who = buyers[0] if buyers else "nobody"
        out.append({"rule": "single_threaded", "cap": caps["single_threaded"],
                    "why": f"only one buyer-side person has been on any call ({who})"})
    for e in m.critical:
        if e.cap_when_not_known is not None and status.get(e.key) != "known":
            out.append({"rule": e.cap_rule, "cap": caps.get(e.cap_rule, e.cap_when_not_known), "why": e.cap_why})
    dated = [l for l in ctx["open_loops"] if l["owner"] in BUYER_OWNERS and l.get("due_date") and l["confidence"] != "low"]
    if not dated and "no_dated_buyer_commitment" in caps:
        out.append({"rule": "no_dated_buyer_commitment", "cap": caps["no_dated_buyer_commitment"],
                    "why": "no open commitment from their side has an owner and a date"})
    known = sum(status.get(k) == "known" for k in m.keys)
    floor = m.min_known
    if floor and known < floor["count"]:
        out.append({"rule": floor["rule"], "cap": caps.get(floor["rule"], floor["cap"]),
                    "why": f"only {known} of {m.count} {m.name} elements are known"})
    # The order the seller has always read them in: voices, critical elements, commitment, coverage.
    rank = {"single_threaded": 0, "no_dated_buyer_commitment": 2, (floor or {}).get("rule"): 3}
    return sorted(out, key=lambda c: rank.get(c["rule"], 1))


def validate(conn, ctx, raw: dict, m=None) -> dict:
    """Deterministic checks on the strategist's answer. No writes."""
    m = m or methodology.active()
    rejected = []
    result = {"deal_id": ctx["deal_id"], "call_id": ctx["call_id"], "summary": _fix(raw["summary"]),
              "today": ctx["today"], "methodology": {"key": m.key, "name": m.name}}

    stakeholders, seen = [], set()
    for s in raw["stakeholders"]:
        s = dict(s)
        pid, plan = _resolve_person(conn, s, ctx)
        conf, notes, entries = judge_entries(s["evidence"], s["confidence"], ctx,
                                             requires_quote=plan["how"] != "contact")
        s["evidence"] = entries
        if plan["how"] == "contact":
            c = plan["contact"]
            s["contact"] = {k: c[k] for k in ("name", "email", "path", "context")}
            s["name"] = s.get("name") or c["name"]
            s["email"] = s.get("email") or c["email"]
            if not any(e.get("found") for e in entries):
                conf = conf_min(conf, "medium")
                notes.append("known from your contact file, not yet heard on a call")
        why = plan.get("why")
        if plan["how"] == "new":
            why = _new_person_named(s, entries, ctx)
        key = pid or ("contact:" + s["email"] if plan["how"] == "contact" else ev.normalize(s.get("name") or s.get("role")))
        if not why and key in seen:
            why = "listed twice"
        if why:
            s["rejected"] = why
            rejected.append({"stakeholder": s.get("name") or s.get("person_id") or s.get("role"), "notes": [why]})
        else:
            seen.add(key)
        s.update(person_id=pid, resolved=plan["how"], validated_confidence=conf, validation_notes=notes)
        for k in ("role",):
            s[k] = _fix(s[k])
        stakeholders.append(s)
    result["stakeholders"] = stakeholders

    by_el = {}
    for item in raw["meddpicc"]:
        if item["element"] not in m.keys:          # the schema already refuses these; a direct caller may not
            rejected.append({"meddpicc": item["element"], "notes": [f"not an element of {m.name}"]})
            continue
        if item["element"] in by_el:
            rejected.append({"meddpicc": item["element"], "notes": ["listed twice; kept the first"]})
            continue
        by_el[item["element"]] = item
    meddpicc = []
    for el in m.keys:
        item = dict(by_el.get(el) or {"element": el, "status": "unknown", "what_we_know": "",
                                       "gap": "The strategist did not assess this element.", "next_question": "",
                                       "evidence": [], "confidence": "low"})
        conf, notes, entries = judge_entries(item["evidence"], item["confidence"], ctx,
                                             requires_quote=item["status"] != "unknown")
        found = any(e.get("found") for e in entries)
        status = item["status"]
        if status == "known" and not (found and CONFIDENCE_RANK[conf] >= CONFIDENCE_RANK["medium"]):
            status = "partial" if found else "unknown"
            notes.append(f"'known' needs a verified quote at medium confidence or better; recorded as {status}")
        elif status == "partial" and not found:
            status = "unknown"
            notes.append("'partial' needs at least one verified quote; recorded as unknown")
        if status != item["status"]:
            rejected.append({"meddpicc": el, "notes": notes[-1:]})
        item.update(status=status, evidence=entries, validated_confidence=conf, validation_notes=notes,
                    next_question=_fix(item["next_question"]), what_we_know=_fix(item["what_we_know"]),
                    gap=_fix(item["gap"]))
        meddpicc.append(item)
    result["meddpicc"] = meddpicc

    buyers = _buyer_side(ctx)
    result["buyer_side"] = buyers
    risks_by_type = {}
    for r in raw["risks"]:
        r = dict(r)
        _, notes, entries = judge_entries(r["evidence"], "high", ctx, requires_quote=False)
        r["evidence"] = [e for e in entries if e.get("found") or not (e.get("quote") or "").strip()]
        dropped = len(entries) - len(r["evidence"])
        r["validation_notes"] = [f"{dropped} cited quote(s) not found and dropped"] if dropped else []
        r.update(source="strategist", description=_fix(r["description"]), mitigation=_fix(r["mitigation"]))
        prev = risks_by_type.get(r["type"])
        if prev is None or SEVERITY_ORDER[r["severity"]] < SEVERITY_ORDER[prev["severity"]]:
            risks_by_type[r["type"]] = r
    if len(buyers) <= 1 and "single_threading" not in risks_by_type:
        risks_by_type["single_threading"] = {
            "type": "single_threading", "severity": "high", "source": "rule", "evidence": [], "validation_notes": [],
            "description": f"Only {buyers[0] if buyers else 'one person'} from their side has been on any call.",
            "mitigation": "Get a second senior person from their side onto the next call."}
    for e in m.elements:
        # The methodology says what not knowing this element means for the deal. Raised from the data,
        # like single-threading, so a model that forgets the risk does not make it go away.
        state = next(x for x in meddpicc if x["element"] == e.key)
        if e.risk_when_unknown and state["status"] == "unknown" and e.risk_when_unknown not in risks_by_type:
            ask = state.get("next_question") or (e.questions[0] if e.questions else "")
            risks_by_type[e.risk_when_unknown] = {
                "type": e.risk_when_unknown, "severity": "high" if e.critical else "medium", "source": "rule",
                "evidence": [], "validation_notes": [],
                "description": f"{e.label} is still unknown: nothing the buyer has said on a call confirms it.",
                "mitigation": f'Ask on the next call: "{ask}"' if ask else f"Get {e.label.lower()} confirmed on the next call."}
    result["risks"] = sorted(risks_by_type.values(), key=lambda r: SEVERITY_ORDER[r["severity"]])

    assessments = []
    for subject in SUBJECTS:
        a = next((x for x in raw["assessments"] if x["subject"] == subject), None)
        if a is None:
            continue
        a = dict(a)
        conf, notes, entries = judge_entries(a["evidence"], a["confidence"], ctx)
        a.update(evidence=entries, validated_confidence=conf, validation_notes=notes,
                 stance=_fix(a["stance"]), rationale=_fix(a["rationale"]))
        assessments.append(a)
    result["assessments"] = assessments

    nba = dict(raw["next_best_action"])
    _, notes, entries = judge_entries(nba["evidence"], "high", ctx, requires_quote=False)
    nba["evidence"] = entries
    if nba.get("by_when"):
        try:
            when = date.fromisoformat(nba["by_when"][:10])
            nba["by_when"] = when.isoformat()
            if when.isoformat() < ctx["today"]:
                notes.append("the proposed date has already passed")
        except ValueError:
            notes.append(f"by_when {nba['by_when']!r} is not a date; dropped")
            nba["by_when"] = None
    for k in ("action", "why_highest_leverage", "expected_effect", "what_would_change_it"):
        nba[k] = _fix(nba[k])
        notes += weekday_mismatches(nba[k], ctx["today"])
    nba["validation_notes"] = notes
    result["next_best_action"] = nba

    h = dict(raw["deal_health"])
    caps = health_caps(ctx, meddpicc, buyers, m)
    ceiling = min([c["cap"] for c in caps] or [100])
    h["model_score"] = h["score"]
    h["model_label"] = h["label"]
    h["score"] = min(h["score"], ceiling)
    h["caps"] = [c for c in caps if c["cap"] < h["model_score"]]
    h["label"] = _band(h["score"])
    h["rationale"] = _fix(h["rationale"])
    if h["caps"]:
        h["rationale"] += " Capped at %d: %s." % (h["score"], "; ".join(c["why"] for c in h["caps"]))
    h["validated_confidence"] = conf_min(h["confidence"], "high")
    result["deal_health"] = h
    result["rejected"] = rejected
    return result


# ---- writes ------------------------------------------------------------------------------

def _meaningful(v) -> bool:
    return v not in (None, "", [], "unknown")


def apply(conn, result: dict, run_id) -> dict:
    """Write a validated strategy through the gate. Returns {subject: strategist assessment id}."""
    deal_id, call_id = result["deal_id"], result["call_id"]
    prov = {"kind": "strategist", "ref": f"run:{run_id}", "call": call_id}
    stamp = now()
    account_id = conn.execute("SELECT account_id FROM deals WHERE node_id=?", (deal_id,)).fetchone()["account_id"]

    for s in result["stakeholders"]:
        if s.get("rejected"):
            continue
        pid = s.get("person_id")
        if not pid:
            pid = s["person_id"] = _create_person(conn, deal_id, account_id, s, call_id)
            s["created_person"] = True
        if not conn.execute("SELECT 1 FROM deal_people WHERE deal_id=? AND person_id=?", (deal_id, pid)).fetchone():
            repo.link_deal_person(conn, deal_id, pid, role=s["role"], actor=ACTOR)
        sid = tables.stakeholder_id(deal_id, pid)
        fields = {k: s[k] for k in tables.GATED_FIELDS["stakeholders"]}
        if conn.execute("SELECT 1 FROM stakeholders WHERE id=?", (sid,)).fetchone():
            fields = {k: v for k, v in fields.items() if _meaningful(v)}    # "unknown" is no opinion, not a claim
        s["outcomes"] = tables.upsert(conn, "stakeholders", sid, {"deal_id": deal_id, "person_id": pid}, fields,
                                      s["validated_confidence"], prov,
                                      {"evidence": s["evidence"], "confidence": s["validated_confidence"],
                                       "run_id": run_id, "updated_at": stamp})

    for m in result["meddpicc"]:
        mid = tables.meddpicc_id(deal_id, m["element"])
        fields = {k: m[k] for k in tables.GATED_FIELDS["meddpicc"]}
        m["outcomes"] = tables.upsert(conn, "meddpicc", mid, {"deal_id": deal_id, "element": m["element"]}, fields,
                                      m["validated_confidence"], prov,
                                      {"evidence": m["evidence"], "confidence": m["validated_confidence"],
                                       "run_id": run_id, "updated_at": stamp})

    listed = set()
    for r in result["risks"]:
        rid = tables.risk_id(deal_id, r["type"])
        listed.add(rid)
        conf = "high" if r["source"] == "rule" else "medium"
        fields = {"severity": r["severity"], "status": "open", "description": r["description"],
                  "mitigation": r["mitigation"]}
        r["outcomes"] = tables.upsert(conn, "deal_risks", rid, {"deal_id": deal_id, "type": r["type"], "first_seen": stamp},
                                      fields, conf, prov,
                                      {"evidence": r["evidence"], "source": r["source"], "run_id": run_id,
                                       "updated_at": stamp})
    tables.migrate_risk_ids(conn)
    for row in conn.execute("SELECT id FROM deal_risks WHERE deal_id=? AND status='open'", (deal_id,)).fetchall():
        if row["id"] in listed:
            continue
        p = conn.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='status'",
                         (row["id"],)).fetchone()
        if p and p["confidence"] == "user_input":
            continue                                   # The seller opened it; only the seller closes it
        gate.propose(conn, gate.Proposed(row["id"], "deal_risks", "status", "cleared",
                                         p["confidence"] if p else "medium",
                                         {**prov, "reason": "no longer raised by the strategist"}), actor=ACTOR)

    h = result["deal_health"]
    nba = result["next_best_action"]
    tables.upsert(conn, "deal_health", tables.health_id(deal_id), {"deal_id": deal_id},
                  {"score": h["score"], "label": h["label"], "rationale": h["rationale"]},
                  h["validated_confidence"], prov,
                  {"model_score": h["model_score"], "caps": h["caps"], "next_best_action": nba,
                   "summary": result["summary"], "call_id": call_id, "run_id": run_id, "updated_at": stamp})
    conn.execute("INSERT INTO deal_health_history(deal_id,score,label,call_id,run_id,created_at) VALUES (?,?,?,?,?,?)",
                 (deal_id, h["score"], h["label"], call_id, run_id, stamp))
    engine._emit(conn, ACTOR, "deal_strategy_applied", node_id=deal_id,
                 after={"run_id": run_id, "score": h["score"], "next_best_action": nba["action"]}, source_id=call_id)
    return apply_assessments(conn, result)


def apply_assessments(conn, result: dict) -> dict:
    """Replace this call's strategist assessments (and the reconciliations that cite them)."""
    deal_id, call_id = result["deal_id"], result["call_id"]
    old = {r["id"] for r in conn.execute("SELECT id FROM assessments WHERE call_id=? AND agent=?", (call_id, ACTOR))}
    if old:
        for r in conn.execute("SELECT id, assessment_ids FROM reconciliations WHERE subject LIKE ?",
                              (f"deal:{deal_id}/%",)).fetchall():
            if old & set(tables._loads(r["assessment_ids"], []) or []):
                conn.execute("DELETE FROM reconciliations WHERE id=?", (r["id"],))
        conn.execute(f"DELETE FROM assessments WHERE id IN ({','.join('?' * len(old))})", tuple(old))
    ids = {}
    for a in result["assessments"]:
        turns = sorted({t for e in a["evidence"] if e.get("call_id") == call_id for t in e.get("turns", [])})
        cur = conn.execute(
            "INSERT INTO assessments(subject,call_id,agent,stance,confidence,rationale,evidence_turns,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"deal:{deal_id}/{a['subject']}", call_id, ACTOR, a["stance"], a["validated_confidence"],
             a["rationale"], json.dumps(turns), now()))
        ids[a["subject"]] = db.insert_id(cur)
    return ids


def _created(result) -> list[str]:
    out = [f"stakeholder:{s.get('name') or s['person_id']}" for s in result["stakeholders"] if not s.get("rejected")]
    out += [f"meddpicc:{m['element']}={m['status']}" for m in result["meddpicc"]]
    out += [f"risk:{r['type']}" for r in result["risks"]]
    out.append(f"health:{result['deal_health']['score']}")
    return out


def _rejected(result) -> list[dict]:
    out = list(result.get("rejected") or [])
    for key in ("stakeholders", "assessments"):
        for item in result[key]:
            if item.get("validation_notes") and not item.get("rejected"):
                out.append({key[:-1]: item.get("name") or item.get("subject") or item.get("role"),
                            "notes": item["validation_notes"],
                            "confidence": f"{item['confidence']}->{item['validated_confidence']}"})
    return out


def run_for_call(conn, call_id, force=False):
    """Run (or reuse) the strategist for the deal of this call. Returns the stored strategy or None."""
    call = repo.get_call(conn, call_id)
    if call is None:
        raise KeyError(call_id)
    deal_id = call["deal_id"]
    if not deal_id:
        return None
    agent = StrategistAgent()
    ctx = history.build(conn, deal_id, call_id, agent.methodology)
    prompt_version, input_sha = shas(agent.system_prompt(ctx), agent.build_prompt(ctx))
    cached, row = tables.latest_strategy(conn, deal_id)
    if not force and cached and row["input_sha"] == input_sha:
        _ensure_materialized(conn, cached)
        return cached
    try:
        out, run_id, prompt_version, input_sha = agent.run(conn, ctx)
    except AgentFailed as exc:
        context.save_artifact(conn, call_id, "strategy", {"failed": str(exc)[:2000], "deal_id": deal_id,
                                                          "rate_limited": exc.rate_limited},
                              None, input_sha, prompt_version)
        conn.commit()
        if exc.rate_limited:                   # the worker defers the event and tries again later
            raise
        return None
    result = validate(conn, ctx, out.model_dump(), agent.methodology)
    conn.execute("SAVEPOINT intel_strategy")
    try:
        ids = apply(conn, result, run_id)
        context.save_artifact(conn, call_id, "strategy", result, run_id, input_sha, prompt_version)
        record_items(conn, run_id, created=_created(result), rejected=_rejected(result))
        conn.execute("RELEASE intel_strategy")
    except Exception as exc:
        conn.execute("ROLLBACK TO intel_strategy")
        conn.execute("RELEASE intel_strategy")
        log.exception("applying the strategy for %s failed", deal_id)
        context.save_artifact(conn, call_id, "strategy",
                              {"failed": f"apply: {type(exc).__name__}: {exc}"[:2000], "deal_id": deal_id},
                              run_id, input_sha, prompt_version)
        conn.commit()
        return None
    conn.commit()
    reconcile.run(conn, deal_id, call_id, ids, result)
    conn.commit()
    return result


def _ensure_materialized(conn, result):
    """A cached strategy whose assessments were deleted (the core re-analysed the call) is written back."""
    if conn.execute("SELECT 1 FROM assessments WHERE call_id=? AND agent=?",
                    (result["call_id"], ACTOR)).fetchone():
        return
    ids = apply_assessments(conn, result)
    conn.commit()
    reconcile.run(conn, result["deal_id"], result["call_id"], ids, result)
    conn.commit()


def run_for_deal(conn, deal_id, force=False):
    call_id = history.anchor_call(conn, deal_id)
    if call_id is None:
        raise LookupError("this deal has no analysed call yet; the strategist needs at least one")
    return run_for_call(conn, call_id, force=force)


def render_text(conn, deal_id) -> str:
    """The stored intelligence for a deal, as the CLI prints it."""
    lines = []
    h = tables.health(conn, deal_id)
    if h:
        lines.append(f"DEAL HEALTH {h['score']}/100 ({h['label']}); the strategist said {h['model_score']}")
        lines += [f"  capped at {c['cap']}: {c['why']}" for c in h["caps"]]
        nba = h["next_best_action"] or {}
        lines += ["", "NEXT BEST ACTION", f"  {nba.get('action')}",
                  f"  owner {nba.get('owner')}{' (' + nba['owner_name'] + ')' if nba.get('owner_name') else ''}"
                  f" | by {nba.get('by_when') or '-'}",
                  f"  why: {nba.get('why_highest_leverage')}", f"  expected: {nba.get('expected_effect')}",
                  f"  would change if: {nba.get('what_would_change_it')}"]
        if h.get("summary"):
            lines += ["", f"SUMMARY  {h['summary']}"]
    lines += ["", "STAKEHOLDERS"]
    for s in tables.stakeholders(conn, deal_id):
        mine = f" | you set {', '.join(sorted(s['user_set']))}" if s["user_set"] else ""
        lines.append(f"  {s['name']:<26} {s.get('position') or '-':<10} influence {s.get('influence') or '-':<7}"
                     f" champion potential {s.get('champion_potential') or '-':<7} can block {s.get('ability_to_block') or '-':<7}"
                     f" [{s.get('confidence') or '-'}{mine}] {s.get('role') or s.get('role_in_deal') or ''}")
    active = methodology.active()
    rows = tables.meddpicc_rows(conn, deal_id, active)
    known = sum(r["status"] == "known" for r in rows)
    lines += ["", f"{active.name.upper()} ({known} of {active.count} known)"]
    for m in rows:
        lines.append(f"  {m['label']:<18} {m['status'] or '-':<8} [{m['confidence'] or '-'}] ask: {m['next_question'] or '-'}")
    lines += ["", "RISKS"]
    lines += [f"  {r['severity']:<8} {r['type']:<28} {r['description']}" for r in tables.risks(conn, deal_id)] or ["  none"]
    lines += ["", "ASSESSMENTS (analyst | strategist | reconciled)"]
    for subject in SUBJECTS:
        full = f"deal:{deal_id}/{subject}"
        latest = {r["agent"]: r["stance"] for r in conn.execute(
            "SELECT agent, stance FROM assessments WHERE subject=? ORDER BY id", (full,))}
        rec = conn.execute("SELECT verdict FROM reconciliations WHERE subject=? ORDER BY id DESC LIMIT 1", (full,)).fetchone()
        lines.append(f"  {subject}: {latest.get('call_analyst', '-')} | {latest.get(ACTOR, '-')}"
                     f" | {rec['verdict'] if rec else 'no disagreement recorded'}")
    open_conflicts = tables.conflicts(conn, deal_id)
    if open_conflicts:
        lines += ["", f"{len(open_conflicts)} open conflict(s) need your decision on the deal page."]
    return "\n".join(lines)


def step(conn, call_id, force=False):
    """Pipeline step 'strategized'. Enrichment only: it never fails the call's pipeline,
    except on a quota refusal, which the worker turns into a deferred retry of this step."""
    call = repo.get_call(conn, call_id)
    if call is None or not call["deal_id"]:
        return
    try:
        run_for_call(conn, call_id, force=force)
    except AgentFailed as exc:
        if exc.rate_limited:
            raise
        conn.rollback()
        log.exception("deal strategy failed for %s", call_id)
    except Exception as exc:
        conn.rollback()
        log.exception("deal strategy failed for %s", call_id)
        context.save_artifact(conn, call_id, "strategy",
                              {"failed": f"{type(exc).__name__}: {exc}"[:2000], "deal_id": call["deal_id"]})
