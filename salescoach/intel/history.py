"""What the Deal Strategist sees: the whole deal, rendered once.

The agent never queries the database. This module collects every analysed
call (summary, validated claims, gaps, the analyst's assessments), the open
and recently closed loops, the people on the deal with anything the seller set by
hand, people at the account from his contact files, the previous strategist
read, and the latest call's full transcript.

Cache discipline: the prompt must not contain anything the strategist's own
last run wrote, or re-running with unchanged inputs would never hit the cache.
So the map is shown only as the seller's edits plus the PREVIOUS anchor call's
strategy, never the current one.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .. import config, repo, seller
from ..orchestrator import context
from . import methodology, tables

_EMAIL_ROW = re.compile(r"\|\s*Email\s*\|\s*([^|\s]+@[^|\s]+)\s*\|", re.I)


def __getattr__(name):
    """`IST` is the seller's zone (Asia/Kolkata unless the profile says otherwise), read on every
    access so a profile edit needs no restart. The name is historical."""
    if name == "IST":
        return seller.zone()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def today_ist() -> str:
    return datetime.now(seller.zone()).date().isoformat()


def cfg() -> dict:
    return config.load("intel")


# ---- contact files --------------------------------------------------------------

def contacts_dir() -> Path:
    return Path(os.path.expanduser(cfg().get("contacts_dir", "~/.claude/contacts")))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def parse_contact(path: Path) -> dict:
    text = path.read_text(errors="replace")
    name = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
    m = _EMAIL_ROW.search(text)
    context_line = ""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("## relationship context"):
            context_line = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
            break
    return {"name": name, "email": m.group(1).lower() if m else None, "context": context_line,
            "path": str(path), "text": text}


def contact_for(person) -> dict | None:
    """A person's contact file: people.contact_file if set, else ~/.claude/contacts/<slug(name)>.md."""
    raw = person.get("contact_file") if isinstance(person, dict) else person["contact_file"]
    candidates = []
    if raw:
        candidates.append(Path(os.path.expanduser(raw)))
    name = person.get("name") if isinstance(person, dict) else person["name"]
    if name:
        candidates.append(contacts_dir() / f"{_slug(name)}.md")
    for path in candidates:
        if path.is_file():
            try:
                return parse_contact(path)
            except OSError:
                continue
    return None


def account_contacts(conn, deal) -> list[dict]:
    """People at the deal's account (by email domain) in the seller's contact files who are not in sales.db yet."""
    domains = set(tables._loads(deal.get("domains"), []) or [])
    d = contacts_dir()
    if not domains or not d.is_dir():
        return []
    known = {r["email"] for r in conn.execute("SELECT email FROM people WHERE email IS NOT NULL")}
    out = []
    for path in sorted(d.glob("*.md")):
        try:
            c = parse_contact(path)
        except OSError:
            continue
        if c["email"] and c["email"].split("@", 1)[1] in domains and c["email"] not in known:
            out.append(c)
    return out


# ---- collect ----------------------------------------------------------------------

def _deal(conn, deal_id):
    row = conn.execute("SELECT d.*, a.name AS account_name, a.domains FROM deals d "
                       "LEFT JOIN accounts a ON a.node_id=d.account_id WHERE d.node_id=?", (deal_id,)).fetchone()
    return dict(row) if row else None


def anchor_call(conn, deal_id):
    """The deal's newest analysed call: what an on-demand strategy run is anchored to."""
    row = conn.execute(
        "SELECT c.node_id FROM calls c WHERE c.deal_id=? AND EXISTS (SELECT 1 FROM artifacts a "
        "WHERE a.call_id=c.node_id AND a.kind='analysis') ORDER BY c.started_at DESC LIMIT 1", (deal_id,)).fetchone()
    return row["node_id"] if row else None


def build(conn, deal_id, call_id, m=None) -> dict:
    m = m or methodology.active()
    deal = _deal(conn, deal_id)
    if deal is None:
        raise KeyError(deal_id)
    s = cfg().get("strategist") or {}
    calls = []
    turns_by_call = {}
    for c in conn.execute("SELECT * FROM calls WHERE deal_id=? ORDER BY started_at", (deal_id,)):
        cid = c["node_id"]
        turns = [dict(t) for t in repo.turns(conn, cid, "final")]
        turns_by_call[cid] = {t["idx"]: t for t in turns}
        summary, _ = context.artifact(conn, cid, "summary")
        analysis, _ = context.artifact(conn, cid, "analysis")
        claims = [dict(r) for r in conn.execute("SELECT * FROM claims WHERE call_id=? ORDER BY id", (cid,))]
        assessments = [dict(r) for r in conn.execute(
            "SELECT * FROM assessments WHERE call_id=? AND agent='call_analyst' ORDER BY id", (cid,))]
        participants = [dict(p) for p in repo.call_participants(conn, cid)]
        unmapped = sorted({t["speaker_cluster"] or "them" for t in turns
                           if t["channel"] == "them" and not t.get("person_id")})
        calls.append({"call": dict(c), "date": context.call_date_ist(c)[0], "summary": summary, "analysis": analysis,
                      "claims": claims, "assessments": assessments, "participants": participants,
                      "unmapped": unmapped, "turn_count": len(turns)})
    analysed = [c for c in calls if c["analysis"]]
    shown = analysed[-int(s.get("max_prior_calls", 12)):]
    anchor = next((c for c in calls if c["call"]["node_id"] == call_id), None)

    people = [dict(p) for p in repo.deal_people(conn, deal_id)]
    me = next((p for p in people if p["is_me"]), None)
    buyers = [p for p in people if not p["is_me"]]
    others = [dict(r) for r in conn.execute(
        "SELECT * FROM people WHERE account_id=? AND is_me=0 AND node_id NOT IN "
        "(SELECT person_id FROM deal_people WHERE deal_id=?)", (deal["account_id"], deal_id))] \
        if deal.get("account_id") else []
    participant_ids = {p["node_id"] for c in calls for p in c["participants"]}
    extra = [dict(r) for r in conn.execute(
        f"SELECT * FROM people WHERE is_me=0 AND node_id IN ({','.join('?' * len(participant_ids))})",
        tuple(participant_ids))] if participant_ids else []
    known_people = {p["node_id"]: p for p in buyers + others + extra}

    prov = tables.provenance_map(conn, deal_id)
    user_fields = {}
    for p in buyers:
        sid = tables.stakeholder_id(deal_id, p["node_id"])
        fields = tables.user_set(prov.get(sid, {}))
        if fields:
            row = conn.execute("SELECT * FROM stakeholders WHERE id=?", (sid,)).fetchone()
            if row:
                user_fields[p["node_id"]] = {f: row[f] for f in fields}
    # Only the ACTIVE methodology's elements: what the seller set on an element of another framework
    # stays stored (and protected) but must not reach a prompt that asks about different elements.
    user_meddpicc = {}
    for r in conn.execute("SELECT * FROM meddpicc WHERE deal_id=?", (deal_id,)):
        if r["element"] not in m.keys:
            continue
        fields = tables.user_set(prov.get(r["id"], {}))
        if fields:
            user_meddpicc[r["element"]] = {f: r[f] for f in fields}
    user_risks = {}
    tables.migrate_risk_ids(conn)
    for r in conn.execute("SELECT * FROM deal_risks WHERE deal_id=?", (deal_id,)):
        fields = tables.user_set(prov.get(r["id"], {}))
        if fields:
            user_risks[r["type"]] = {f: r[f] for f in fields}

    open_loops = [dict(r) for r in conn.execute(
        "SELECT * FROM loops WHERE deal_id=? AND status IN ('open','waiting') AND review_state!='rejected' "
        "ORDER BY due_date IS NULL, due_date, created_at", (deal_id,))]
    closed_loops = [dict(r) for r in conn.execute(
        "SELECT * FROM loops WHERE deal_id=? AND status IN ('done','cancelled','superseded') "
        "AND review_state!='rejected' ORDER BY closed_at DESC LIMIT ?", (deal_id, int(s.get("closed_loops_shown", 10))))]
    prior, prior_row = tables.latest_strategy(conn, deal_id, exclude_call=call_id)
    # Phase F2: persona/objection patterns PROMOTED across deals (none before 8 deals carry one), limited
    # to buckets seen on this deal. Rendered as priors, never as evidence; [] until something is promoted.
    from ..learning import feedback
    priors = feedback.select(conn, "strategist", deal_id)
    return {
        "learned_priors": priors,
        "deal_id": deal_id, "call_id": call_id, "deal": deal, "today": today_ist(), "me": me,
        "calls": calls, "shown_calls": shown, "anchor": anchor, "turns_by_call": turns_by_call,
        "buyers": buyers, "other_people": others, "known_people": known_people,
        "contacts": account_contacts(conn, deal),
        "user_fields": user_fields, "user_meddpicc": user_meddpicc, "user_risks": user_risks,
        "open_loops": open_loops, "closed_loops": closed_loops,
        "prior": prior, "prior_call": prior_row["call_id"] if prior_row else None,
        "max_turns": int(s.get("max_transcript_turns", 400)),
        "methodology": m.key,
    }


# ---- render --------------------------------------------------------------------

def _q(text, limit=400):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _people_block(ctx) -> str:
    lines = []
    for p in ctx["buyers"]:
        on_calls = [c["call"]["node_id"] for c in ctx["calls"] if any(x["node_id"] == p["node_id"] for x in c["participants"])]
        line = (f"- {p['node_id']} | {p['name']}{' <' + p['email'] + '>' if p.get('email') else ''}"
                f" | title {p.get('title') or '-'} | role in deal {p.get('role_in_deal') or '-'}"
                f" | on calls: {', '.join(on_calls) or 'none'}")
        uf = ctx["user_fields"].get(p["node_id"])
        if uf:
            line += f"\n  SET BY {seller.first_name_upper()} (do not contradict without new evidence): " + "; ".join(
                f"{k}={v}" for k, v in uf.items())
        lines.append(line)
    return "\n".join(lines) or f"(none besides {seller.first_name() or 'the seller'})"


def _others_block(ctx) -> str:
    lines = [f"- {p['node_id']} | {p['name']}{' <' + p['email'] + '>' if p.get('email') else ''} | title {p.get('title') or '-'}"
             for p in ctx["other_people"]]
    return "\n".join(lines) or "(none)"


def _contacts_block(ctx) -> str:
    lines = [f"- contact:{c['email']} | {c['name']} | {_q(c['context'], 200) or 'no context line'}"
             for c in ctx["contacts"]]
    return "\n".join(lines) or "(none)"


def _call_block(c, anchor_id) -> str:
    call = c["call"]
    cid = call["node_id"]
    head = (f"### {cid} | {call.get('title') or '(untitled)'} | {c['date']} | source {call['source']}"
            f" | transcript quality {call.get('quality_score') if call.get('quality_score') is not None else '-'}"
            f"{' | THIS IS THE LATEST CALL (full transcript below)' if cid == anchor_id else ''}")
    lines = [head]
    names = [p["name"] + (" (ME)" if p["is_me"] else "") for p in c["participants"]]
    lines.append(f"Participants: {', '.join(names) or 'not recorded'}"
                 + (f". Unmapped buyer-side speakers: {', '.join(c['unmapped'])}" if c["unmapped"] else ""))
    a = c["analysis"] or {}
    if a.get("verdict"):
        lines.append(f"Analyst verdict: {a['verdict']['label']}: {a['verdict']['one_line']}")
    summ = c["summary"] or {}
    if summ.get("what_happened"):
        lines.append(f"What happened: {_q(summ['what_happened'], 800)}")
    for d in summ.get("decisions") or []:
        lines.append(f"- decision: {_q(d['text'])} (turns {d['evidence_turns']})")
    for cm in summ.get("commitments") or []:
        lines.append(f"- commitment [{cm['owner']}{' ' + cm['owner_name'] if cm.get('owner_name') else ''}]: "
                     f"{_q(cm['text'])} (turns {cm['evidence_turns']})")
    if summ.get("next_step"):
        lines.append(f"- agreed next step: {_q(summ['next_step']['text'])} (turns {summ['next_step']['evidence_turns']})")
    if a.get("what_changed"):
        lines.append("What changed (analyst): " + "; ".join(_q(x, 200) for x in a["what_changed"]))
    if c["claims"]:
        lines.append("Validated claims (confidence already checked against the transcript):")
        for cl in c["claims"]:
            lines.append(f"- [{cl['kind']}/{cl['confidence']}] {cl['subject']}: {_q(cl['statement'], 300)}"
                         f" | turns {cl['evidence_turns']} | \"{_q(cl['evidence_quote'], 240)}\"")
    for g in a.get("gaps") or []:
        lines.append(f"- gap [{g['lens']}/{g['element']}]: {_q(g['missing'], 250)} | ask: {_q(g['question_to_ask'], 200)}")
    for asm in c["assessments"]:
        lines.append(f"- analyst assessment {asm['subject'].split('/', 1)[-1]}: {asm['stance']} ({asm['confidence']})")
    return "\n".join(lines)


def _loops_block(ctx) -> str:
    today = ctx["today"]
    lines = []
    for l in ctx["open_loops"]:
        overdue = " OVERDUE" if l.get("due_date") and l["due_date"] < today else ""
        lines.append(f"- {l['node_id']} | {l['type']} | owner {l['owner']}{' (' + l['owner_name'] + ')' if l.get('owner_name') else ''}"
                     f" | due {l.get('due_date') or '-'}{overdue} | {l['status']}/{l['review_state']} | conf {l['confidence']}"
                     f" | from {l.get('call_id') or '-'} | {_q(l['description'], 220)}")
    closed = [f"- {l['status']} {(l.get('closed_at') or '')[:10]} | owner {l['owner']} | {_q(l['description'], 160)}"
              for l in ctx["closed_loops"]]
    return ("\n".join(lines) or "(none)") + ("\nRecently closed:\n" + "\n".join(closed) if closed else "")


def _prior_block(ctx, m) -> str:
    p = ctx["prior"]
    if not p:
        return "(no earlier strategist read)"
    lines = [f"From the strategist run anchored on {ctx['prior_call']}:"]
    if p.get("summary"):
        lines.append(f"Summary: {_q(p['summary'], 600)}")
    h = p.get("deal_health") or {}
    if h:
        lines.append(f"Health: {h.get('score')} ({h.get('label')})")
    for s in p.get("stakeholders") or []:
        if s.get("rejected"):
            continue
        lines.append(f"- {s.get('person_id') or s.get('name')}: {s.get('role')} | position {s.get('position')} | "
                     f"influence {s.get('influence')} | champion potential {s.get('champion_potential')}")
    for item in p.get("meddpicc") or []:
        if item["element"] not in m.keys:              # a read made under another methodology
            continue
        lines.append(f"- {m.label(item['element'])}: {item['status']} | gap: {_q(item.get('gap'), 160)}")
    for r in p.get("risks") or []:
        lines.append(f"- risk {r['type']} ({r['severity']}): {_q(r['description'], 160)}")
    nba = p.get("next_best_action") or {}
    if nba:
        lines.append(f"Next best action then: {_q(nba.get('action'), 200)} (by {nba.get('by_when') or '-'})")
    return "\n".join(lines)


def _user_state_block(ctx, m) -> str:
    lines = []
    for el, f in ctx["user_meddpicc"].items():
        if el not in m.keys:
            continue
        lines.append(f"- {m.name} {m.label(el)}: " + "; ".join(f"{k}={v}" for k, v in f.items()))
    for t, f in ctx["user_risks"].items():
        lines.append(f"- risk {t}: " + "; ".join(f"{k}={v}" for k, v in f.items()))
    return "\n".join(lines) or "(nothing)"


def render(ctx, m=None) -> str:
    m = m or methodology.active()
    deal = ctx["deal"]
    anchor = ctx["anchor"]
    anchor_id = ctx["call_id"]
    parts = [
        f"TODAY: {ctx['today']} ({seller.tz_label()}). Resolve every date against this.",
        f"DEAL: {deal['name']} | account {deal.get('account_name') or '-'} | stage {deal.get('stage') or '-'} | "
        f"status {deal['status']} | recorded next step {deal.get('next_step') or '-'} | close target {deal.get('close_target') or '-'}",
        "",
        "STAKEHOLDERS ON RECORD (use these ids)",
        _people_block(ctx),
        "",
        "OTHER PEOPLE AT THIS ACCOUNT (in the database, not yet on the deal)",
        _others_block(ctx),
        "",
        f"CONTACTS AT THIS ACCOUNT (from {seller.first_name() or 'the seller'}'s own contact files; never heard on a call unless a call says so)",
        _contacts_block(ctx),
        "",
        f"{seller.first_name_upper()}'S OWN SETTINGS ON {m.name.upper()} AND RISKS (do not contradict without new evidence)",
        _user_state_block(ctx, m),
        "",
        "OPEN LOOPS (who owes what)",
        _loops_block(ctx),
        "",
        "PREVIOUS STRATEGIST READ",
        _prior_block(ctx, m),
        "",
    ]
    from ..learning import feedback
    priors = feedback.priors_block(ctx.get("learned_priors"))
    if priors:                                          # nothing at all is added until a prior is promoted
        parts += [priors, ""]
    parts.append("CALL HISTORY (oldest first)")
    for c in ctx["shown_calls"]:
        parts += [_call_block(c, anchor_id), ""]
    if anchor and not anchor["analysis"]:
        parts += [_call_block(anchor, anchor_id), ""]
    if anchor:
        turns = list(ctx["turns_by_call"][anchor_id].values())[:ctx["max_turns"]]
        tctx = {"turns": turns, "participants": anchor["participants"], "deal_people": ctx["buyers"]}
        parts += [f"LATEST CALL TRANSCRIPT ({anchor_id})",
                  "Markers: {garbled} and {partial} turns were mangled by speech recognition; {bleed} may be the "
                  "other side's audio. THEM:<label> is a buyer-side speaker not yet mapped to a person.",
                  context.transcript_block(tctx)]
    return "\n".join(parts)


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
