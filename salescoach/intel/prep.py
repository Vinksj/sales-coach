"""Pre-call prep: everything the seller needs in the ten minutes before a call.

Public entry point for Phase 4 (called when a meeting with the deal appears on
the calendar):

    generate(conn, deal_id, meeting_title=None, attendees=(), when=None) -> prep_briefs.id

Most of the brief is assembled deterministically from what is already stored:
open loops (overdue first, who owes what), the gaps in the active sales
methodology (MEDDPICC unless the org chose another) with the exact questions,
the stakeholder map plus the first lines of each contact file, the top risks, the next best action, the active coaching priority, what changed
since the last call, and "what worked before" from similar past moments
(embeddings). One small model call writes the objective, the opening and the
close in his voice; if it fails, a plain template stands in, so a brief is
always produced.
"""
import json
import logging

from .. import repo, seller
from ..agents.base import AgentFailed
from ..memory import patterns
from ..orchestrator import context
from ..store.stores import now
from ..validators import evidence as ev
from ..validators import voice_lint
from . import coach, embed, history, methodology, strategist, tables
from .agentkit import PROMPTS, IntelAgent
from .schemas import PrepWriting

log = logging.getLogger("salescoach.intel")


class PrepAgent(IntelAgent):
    name = "prep_writer"
    schema = PrepWriting

    def system_prompt(self, ctx):
        return methodology.render_prompt(PROMPTS / f"{self.name}.md")

    def build_prompt(self, ctx):
        return render_facts(ctx["brief"])

    def input_refs(self, ctx):
        # The learned patterns this brief carried (phase F2), so their effect can be measured later.
        return {**super().input_refs(ctx), "patterns": [p["id"] for p in ctx["brief"].get("learned") or []]}


# ---- assembly ----------------------------------------------------------------------

def _owner_label(l) -> str:
    if l["owner"] == "me":
        return "You"
    if l.get("owner_name"):
        return l["owner_name"]
    return {"prospect": "Them", "mutual": "Both", "internal": seller.team_label()}.get(l["owner"], l["owner"])


def _loops(conn, deal_id, today) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT l.*, c.title AS call_title FROM loops l LEFT JOIN calls c ON c.node_id=l.call_id "
        "WHERE l.deal_id=? AND l.status IN ('open','waiting') AND l.review_state!='rejected'", (deal_id,))]
    for l in rows:
        l["overdue"] = bool(l.get("due_date") and l["due_date"] < today)
        l["who"] = _owner_label(l)
    # Overdue first, most important of those first; then the rest by due date.
    rows.sort(key=lambda l: (not l["overdue"], tables.SEVERITY_ORDER.get(l["priority"], 4) if l["overdue"] else 0,
                             l.get("due_date") is None, l.get("due_date") or "",
                             tables.SEVERITY_ORDER.get(l["priority"], 4)))
    return [{k: l.get(k) for k in ("node_id", "description", "who", "owner", "due_date", "overdue", "status",
                                    "review_state", "priority", "confidence", "call_id", "call_title")} for l in rows]


def _gaps(conn, deal_id, last_analysis, m=None) -> list[dict]:
    """What is still missing, most important first: the methodology's gap order (critical elements
    first unless it says otherwise). An element the strategist left without a question gets the
    methodology's own. Before any strategist run, the analyst's gaps through this methodology's lens."""
    m = m or methodology.active()
    rows = [r for r in tables.meddpicc_rows(conn, deal_id, m) if r["status"]]
    if rows:
        gaps = [{"element": r["element"], "label": r["label"], "status": r["status"], "gap": r["gap"],
                 "question": r["next_question"] or next(iter(m.element(r["element"]).questions), ""),
                 "source": "strategist"} for r in rows if r["status"] != "known"]
        return sorted(gaps, key=lambda g: m.gap_rank(g["element"]))
    return [{"element": g.get("element"), "label": g.get("element"), "status": "gap", "gap": g.get("missing"),
             "question": g.get("question_to_ask"), "source": "analyst"}
            for g in (last_analysis or {}).get("gaps", []) if g.get("lens") == m.lens]


def _stakeholders(conn, deal_id, attendees) -> tuple[list, list]:
    people = tables.stakeholders(conn, deal_id)
    chars = int(history.cfg().get("contact_chars", 1500))
    wanted = [a.strip().lower() for a in attendees if a and a.strip()]
    matched, out = set(), []
    for s in people:
        attending = any(a == (s.get("email") or "").lower() or a == (s.get("name") or "").lower() for a in wanted)
        if attending:
            matched |= {a for a in wanted if a in ((s.get("email") or "").lower(), (s.get("name") or "").lower())}
        c = history.contact_for(s)
        out.append({"person_id": s["person_id"], "name": s["name"], "email": s.get("email"),
                    "title": s.get("title"), "role": s.get("role") or s.get("role_in_deal"),
                    "position": s.get("position"), "influence": s.get("influence"),
                    "champion_potential": s.get("champion_potential"), "ability_to_block": s.get("ability_to_block"),
                    "incentives": s["incentives"], "concerns": s["concerns"], "attending": attending,
                    "contact": c["text"][:chars] if c else None, "contact_path": c["path"] if c else None})
    out.sort(key=lambda s: not s["attending"])
    return out, [a for a in wanted if a not in matched]


def _strategy_changes(conn, deal_id) -> list[str]:
    latest, row = tables.latest_strategy(conn, deal_id)
    if not latest:
        return []
    prior, _ = tables.latest_strategy(conn, deal_id, exclude_call=row["call_id"])
    if not prior:
        return []
    out = []
    names = {r["node_id"]: r["name"] for r in conn.execute("SELECT node_id, name FROM people")}
    before = {s.get("person_id"): s for s in prior.get("stakeholders", []) if not s.get("rejected")}
    for s in latest.get("stakeholders", []):
        if s.get("rejected"):
            continue
        b = before.get(s.get("person_id"))
        who = names.get(s.get("person_id"), s.get("name") or "someone")
        if b is None:
            out.append(f"New on the map: {who} ({s['role']}).")
        elif b.get("position") != s.get("position"):
            out.append(f"{who}: {b.get('position')} to {s.get('position')}.")
    # Either strategy may have been made under another methodology: compare only elements both have,
    # and never look a label up in a way that can raise on a key this install no longer uses.
    pm = {x["element"]: x["status"] for x in prior.get("meddpicc", [])}
    for x in latest.get("meddpicc", []):
        if pm.get(x["element"]) and pm[x["element"]] != x["status"]:
            out.append(f"{methodology.label_for(x['element'])}: {pm[x['element']]} to {x['status']}.")
    ph, lh = (prior.get("deal_health") or {}).get("score"), (latest.get("deal_health") or {}).get("score")
    if ph is not None and lh is not None and ph != lh:
        out.append(f"Deal health {ph} to {lh}.")
    pr = {r["type"] for r in prior.get("risks", [])}
    lr = {r["type"] for r in latest.get("risks", [])}
    out += [f"New risk: {t.replace('_', ' ')}." for t in sorted(lr - pr)]
    out += [f"Risk no longer raised: {t.replace('_', ' ')}." for t in sorted(pr - lr)]
    return out


def _worked_before(conn, query, deal_id) -> dict:
    try:
        status = embed.index_pending(conn)
    except Exception as exc:                       # the brief must not fail on the index
        log.warning("embedding index failed: %s", exc)
        status = {"status": "error"}
    hits = embed.similar(conn, query, k=12, entity_types=("observation", "window", "insight"))
    verdicts = {}
    items = []
    for h in hits:
        why = None
        if h["entity_type"] == "observation":
            row = conn.execute("SELECT polarity, tag FROM seller_observations WHERE id=?", (int(h["entity_id"]),)).fetchone()
            if row and row["polarity"] == "strength":
                why = f"a strength: {row['tag'].replace('_', ' ')}"
        elif h["entity_type"] == "insight":
            why = "the coaching insight from a similar call"
        elif h["entity_type"] == "window":
            if h["call_id"] not in verdicts:
                a, _ = context.artifact(conn, h["call_id"], "analysis")
                verdicts[h["call_id"]] = ((a or {}).get("verdict") or {}).get("label")
            if verdicts[h["call_id"]] == "advanced":
                why = "a moment from a call that advanced its deal"
        if why:
            call = repo.get_call(conn, h["call_id"]) if h["call_id"] else None
            items.append({**h, "why": why, "call_title": call["title"] if call else None,
                          "same_deal": bool(call and call["deal_id"] == deal_id)})
        if len(items) >= 4:
            break
    # "moments", not "items": Jinja resolves brief.worked_before.items to dict.items first.
    return {"status": status.get("status"), "moments": items}


def assemble(conn, deal_id, meeting_title=None, attendees=(), when=None) -> dict:
    deal = conn.execute("SELECT d.*, a.name AS account_name FROM deals d LEFT JOIN accounts a "
                        "ON a.node_id=d.account_id WHERE d.node_id=?", (deal_id,)).fetchone()
    if deal is None:
        raise KeyError(deal_id)
    today = history.today_ist()
    last_id = history.anchor_call(conn, deal_id)
    last = None
    analysis = None
    if last_id:
        call = repo.get_call(conn, last_id)
        analysis, _ = context.artifact(conn, last_id, "analysis")
        summary, _ = context.artifact(conn, last_id, "summary")
        last = {"call_id": last_id, "title": call["title"], "date": context.call_date_ist(call)[0],
                "verdict": (analysis or {}).get("verdict"),
                "next_step": ((summary or {}).get("next_step") or {}).get("text"),
                "what_changed": (analysis or {}).get("what_changed") or []}
    h = tables.health(conn, deal_id)
    stakeholders, unknown_attendees = _stakeholders(conn, deal_id, attendees)
    loops = _loops(conn, deal_id, today)
    m = methodology.active()
    gaps = _gaps(conn, deal_id, analysis, m)
    risks = [{"type": r["type"], "severity": r["severity"], "description": r["description"],
              "mitigation": r["mitigation"]} for r in tables.risks(conn, deal_id)][:3]
    prio = patterns.active_priority(conn)              # skips what the user marked wrong / retired / no-prompt
    report = coach.latest(conn)
    reported = (report or {}).get("priority") or {}
    if reported.get("tag") in patterns.suppressed_tags(conn):
        report = {**report, "priority": {}, "say_differently": []}    # a verdict after the report was written
        reported = {}
    coaching = None
    if prio or reported or (report or {}).get("say_differently"):
        coaching = {"name": prio["name"] if prio else None,
                    "intervention": prio["recommended_intervention"] if prio else None,
                    "frequency": prio["frequency"] if prio else None, "calls_window": prio["calls_window"] if prio else None,
                    "practice": reported.get("practice"), "practice_tag": reported.get("tag"),
                    "say": next(iter((report or {}).get("say_differently") or []), None)}
    # Phase F2: what the learning layer holds as ACTIVE (3+ calls over 2+ deals, or confirmed by the seller),
    # weaknesses first, then one strength. Empty until something is learned; then the old path above is
    # still what the brief falls back to for the practice line.
    from ..learning import feedback
    learned = [{k: p.get(k) for k in ("id", "key", "summary", "polarity", "label", "confirmed", "seen", "intervention")}
               for p in feedback.select(conn, "prep", deal_id)]
    unmapped = conn.execute(
        "SELECT COUNT(DISTINCT t.call_id) FROM turns t JOIN calls c ON c.node_id=t.call_id WHERE c.deal_id=? "
        "AND t.tier='final' AND t.channel='them' AND t.person_id IS NULL", (deal_id,)).fetchone()[0]
    nba = (h or {}).get("next_best_action")
    query = " ".join(x for x in [(nba or {}).get("action"), *(g["question"] or "" for g in gaps[:2]),
                                 *(r["description"] or "" for r in risks[:1])] if x)
    return {
        "deal_id": deal_id, "deal_name": deal["name"], "account": deal["account_name"], "today": today,
        "methodology": {"key": m.key, "name": m.name, "lens": m.lens},
        "meeting": {"title": meeting_title, "when": when, "attendees": list(attendees),
                    "unknown_attendees": unknown_attendees},
        "last_call": last, "changes": _strategy_changes(conn, deal_id),
        "health": {"score": h["score"], "label": h["label"], "summary": h.get("summary")} if h else None,
        "next_best_action": nba, "loops": loops, "gaps": gaps, "stakeholders": stakeholders, "risks": risks,
        "coaching": coaching, "learned": learned,
        "worked_before": _worked_before(conn, query or deal["name"], deal_id),
        "unmapped_speaker_calls": unmapped,
    }


def methodology_name(b) -> str:
    """The methodology a brief was made under. Briefs stored before methodologies were data have no
    record of it; they were all MEDDPICC."""
    return (b.get("methodology") or {}).get("name") or "MEDDPICC"


def render_facts(b) -> str:
    """The brief as the prep writer sees it (and as the CLI prints it)."""
    lines = [f"TODAY: {b['today']}", f"DEAL: {b['deal_name']} ({b.get('account') or '-'})"]
    m = b["meeting"]
    lines.append(f"MEETING: {m.get('title') or 'next call'} | when {m.get('when') or 'not set'} | attendees "
                 f"{', '.join(m.get('attendees') or []) or 'not given'}")
    if m.get("unknown_attendees"):
        lines.append(f"Not on the stakeholder map yet: {', '.join(m['unknown_attendees'])}")
    lc = b.get("last_call")
    if lc:
        v = lc.get("verdict") or {}
        lines.append(f"LAST CALL: {lc['title']} on {lc['date']} | verdict {v.get('label', '-')}: {v.get('one_line', '')}")
        if lc.get("next_step"):
            lines.append(f"Agreed next step then: {lc['next_step']}")
        lines += [f"- changed: {x}" for x in lc.get("what_changed") or []]
    lines += [f"- since then: {x}" for x in b.get("changes") or []]
    if b.get("health"):
        lines.append(f"HEALTH: {b['health']['score']} ({b['health']['label']}). {b['health'].get('summary') or ''}")
    nba = b.get("next_best_action")
    if nba:
        lines.append(f"NEXT BEST ACTION: {nba['action']} | owner {nba['owner']} | by {nba.get('by_when') or '-'}"
                     f" | why: {nba['why_highest_leverage']}")
    lines.append("OPEN LOOPS (overdue first):")
    lines += [f"- {'OVERDUE ' if l['overdue'] else ''}{l['who']}: {l['description']} (due {l.get('due_date') or '-'})"
              for l in b["loops"]] or ["- none"]
    lines.append(f"{methodology_name(b).upper()} GAPS AND THE QUESTION TO ASK:")
    lines += [f"- {g['label']} ({g['status']}): {g['gap']} | ask: {g['question']}" for g in b["gaps"]] or ["- none"]
    lines.append("STAKEHOLDERS:")
    for s in b["stakeholders"]:
        lines.append(f"- {s['name']}{' (attending)' if s['attending'] else ''} | {s.get('role') or s.get('title') or '-'}"
                     f" | position {s.get('position') or 'unknown'} | influence {s.get('influence') or '-'}"
                     f" | concerns {', '.join(s['concerns']) or '-'}")
    lines.append("TOP RISKS:")
    lines += [f"- {r['type']} ({r['severity']}): {r['description']} | mitigation: {r['mitigation']}"
              for r in b["risks"]] or ["- none"]
    c = b.get("coaching")
    learned = b.get("learned") or []
    top = next((p for p in learned if p.get("polarity") == "weakness"), None)
    if top:
        # The learned top weakness IS the priority. The coach report's practice line is kept only when it
        # was written about the same habit; about another one it would point the seller two ways.
        practice = (c or {}).get("practice") if (c or {}).get("practice_tag") == top["key"] else None
        lines.append(f"COACHING PRIORITY: {top['summary']} | {top.get('intervention') or ''} | "
                     f"practise: {practice or '-'}")
    elif c:
        lines.append(f"COACHING PRIORITY: {c.get('name') or '-'} | {c.get('intervention') or ''} | "
                     f"practise: {c.get('practice') or '-'}")
    if learned:
        from ..learning import feedback
        lines += feedback.prep_lines(learned)
    wb = b.get("worked_before") or {}
    for w in wb.get("moments") or wb.get("items") or []:          # "items": briefs stored before the rename
        lines.append(f"WORKED BEFORE ({w['why']}, {w.get('call_title') or w['call_id']}): {ev.normalize(w['text'])[:300]}")
    return "\n".join(lines)


def fallback_writing(b) -> dict:
    nba = b.get("next_best_action") or {}
    gap = next(iter(b["gaps"]), None)
    objective = nba.get("action") or (f"Get an answer to: {gap['question']}" if gap and gap.get("question")
                                      else "Agree one next step with an owner and a date.")
    last = (b.get("last_call") or {}).get("next_step")
    opening = "Thanks for making time. " + (f"Last time we agreed: {last} " if last else "") + \
        "I'd like to use today to settle one thing and leave with a clear next step."
    if nba.get("owner") == "me":
        close = f"So I'll {nba['action'][0].lower() + nba['action'][1:]}" + (f" by {nba['by_when']}." if nba.get("by_when") else ".")
    elif nba:
        close = f"Can we agree that {nba.get('owner_name') or 'you'} will take this forward" + \
            (f" by {nba['by_when']}?" if nba.get("by_when") else ", and put a date on it now?")
    else:
        close = "Before we end: who does what next, and by when? Let's put the date in now."
    return {"objective": objective, "objective_why": nba.get("why_highest_leverage") or "",
            "opening": opening, "close": close,
            "watch_for": [r["description"] for r in b["risks"][:2] if r.get("description")]}


def generate(conn, deal_id, meeting_title=None, attendees=(), when=None, use_llm=True) -> int:
    """Build and store a prep brief for the deal's next call. Returns the prep_briefs row id."""
    brief = assemble(conn, deal_id, meeting_title, attendees, when)
    writing, run_id, source = None, None, "fallback"
    if use_llm:
        try:
            out, run_id, _, _ = PrepAgent().run(conn, {"deal_id": deal_id, "brief": brief})
            writing = {k: (voice_lint.autofix(v) if isinstance(v, str) else [voice_lint.autofix(x) for x in v])
                       for k, v in out.model_dump().items()}
            source = "model"
        except AgentFailed as exc:
            log.warning("prep writer failed for %s: %s", deal_id, exc)
            brief["writing_error"] = str(exc)[:300]
    brief["writing"] = writing or fallback_writing(brief)
    brief["writing_source"] = source
    brief["writing_notes"] = [n for v in brief["writing"].values()
                              for t in (v if isinstance(v, list) else [v])
                              for n in strategist.weekday_mismatches(t, brief["today"])]
    cur = conn.execute(
        "INSERT INTO prep_briefs(deal_id,meeting_title,meeting_at,attendees,run_id,json,created_at) VALUES (?,?,?,?,?,?,?)",
        (deal_id, meeting_title, when, json.dumps(list(attendees)), run_id, json.dumps(brief), now()))
    conn.commit()
    return cur.lastrowid


def get(conn, brief_id=None, deal_id=None) -> dict | None:
    if brief_id is not None:
        row = conn.execute("SELECT * FROM prep_briefs WHERE id=?", (brief_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM prep_briefs WHERE deal_id=? ORDER BY id DESC LIMIT 1", (deal_id,)).fetchone()
    if row is None:
        return None
    brief = json.loads(row["json"])
    brief.update(id=row["id"], created_at=row["created_at"], run_id=row["run_id"])
    return brief


def render_text(b) -> str:
    w = b["writing"]
    head = [f"PREP: {b['deal_name']} | {b['meeting'].get('title') or 'next call'}",
            f"Objective: {w['objective']}", f"Why: {w.get('objective_why') or '-'}",
            f"Open with: {w['opening']}", f"Close with: {w['close']}"]
    head += [f"Watch for: {x}" for x in w.get("watch_for") or []]
    return "\n".join(head) + "\n\n" + render_facts(b)
