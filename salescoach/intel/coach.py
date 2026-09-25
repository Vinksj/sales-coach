"""The longitudinal coach: how the seller sells, across calls.

memory/patterns.py counts: frequency, calls seen, trend. Those numbers are the
truth and the model never recomputes them. It gets them verbatim, refers to
patterns only by tag, and the report stores a snapshot of the numbers next to
its narrative. Any percentage or "N of M calls" the model writes that is not
in the data is removed. The trajectory label (improving, worsening, mixed,
stable, insufficient data) is decided here from the trend column; the model
only explains it.

Inputs: seller_patterns, recent observations with their quotes, the per-call
coaching insights, and Phase 2's `nudges` table if it exists (read
generically, since its columns are not ours to fix). Every call it cites is
checked: unknown calls are dropped, quotes not found in the cited turns lose
the quote.

Regenerated after each analysed call once there are enough calls, after a
review, and on demand. Cached on its input hash.
"""
import json
import logging
import re
from collections import Counter

from .. import repo
from ..agents.base import AgentFailed
from ..memory import patterns
from ..store import stores
from ..store import db
from ..validators import evidence as ev
from ..validators import voice_lint
from . import history
from .agentkit import IntelAgent, shas
from .schemas import CoachReport

log = logging.getLogger("salescoach.intel")
_NUMBER = re.compile(r"\b(\d{1,3})\s?(%|percent\b)|\b(\d+)\s+(?:of|out of)\s+(?:the\s+last\s+)?(\d+)\b", re.I)


def _cfg():
    return history.cfg().get("coach") or {}


def analysed_calls(conn) -> list[dict]:
    """The coached seller's own analysed calls (a manager can read their team's too; this is one seller's)."""
    return [dict(r) for r in conn.execute(
        "SELECT c.node_id, c.title, c.started_at, c.deal_id FROM calls c WHERE c.owner_id=? AND EXISTS "
        "(SELECT 1 FROM artifacts a WHERE a.call_id=c.node_id AND a.kind='analysis') ORDER BY c.started_at DESC",
        (_owner(conn),))]


def trajectory(rows) -> tuple[str, dict]:
    """Deterministic: from the trend column of active and candidate patterns."""
    detail = Counter()
    for p in rows:
        detail[f"{p['polarity']}_{p['trend']}"] += 1
    good = detail["weakness_improving"] + detail["strength_improving"]
    bad = detail["weakness_worsening"] + detail["strength_worsening"]
    judged = good + bad + detail["weakness_stable"] + detail["strength_stable"]
    if judged == 0:
        label = "insufficient_data"
    elif good and bad:
        label = "mixed"
    elif good:
        label = "improving"
    elif bad:
        label = "worsening"
    else:
        label = "stable"
    return label, dict(detail)


def _nudges(conn):
    """Phase 2's nudges, read defensively: the table may not exist (plugins off, or Phase 2 not built)
    and its columns are Phase 2's. Only nudges the seller actually saw on a live call count: Phase 2 also
    stores every suppressed candidate and every replay, which say nothing about his adherence."""
    if not conn.table_exists("nudges"):
        return None
    cols = conn.columns("nudges")
    where = []
    if "shown" in cols:
        where.append("shown=1")
    if "mode" in cols:
        where.append("mode='live'")
    params = []
    if "owner_id" in cols:                              # the coached seller's own (see build())
        where.append("owner_id=?")
        params.append(_owner(conn))
    sql = "SELECT * FROM nudges" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 50"
    rows = [dict(r) for r in conn.execute(sql, params)]
    outcome = next((c for c in ("outcome", "adherence", "followed", "acted_on", "result", "status") if c in cols), None)
    text = next((c for c in ("text", "message", "nudge", "prompt", "body", "suggestion") if c in cols), None)
    counts = dict(Counter(str(r[outcome]) for r in rows if r.get(outcome) not in (None, ""))) if outcome else {}
    judged = counts.get("followed", 0) + counts.get("ignored", 0)
    adherence = round(100 * counts.get("followed", 0) / judged) if judged else None
    return {"columns": cols, "rows": rows, "outcome_col": outcome, "text_col": text, "counts": counts,
            "shown": len(rows), "adherence_pct": adherence}


def build(conn) -> dict:
    """Everything the report is made of, and only the coached seller's own rows (_owner: the acting user). Every
    read names the owner, not only the row-level policies: a manager's interactive session may READ the team's
    observations, analyses and nudges, and a report about the manager must never be built from them."""
    c = _cfg()
    owner = _owner(conn)
    calls = analysed_calls(conn)
    gone = patterns.suppressed_tags(conn)              # the user's verdicts on the learning page hold here too
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM seller_patterns WHERE owner_id=? AND status!='retired' ORDER BY polarity, frequency DESC, "
        "calls_seen DESC", (owner,)) if r["tag"] not in gone]
    snapshot = {p["tag"]: {k: p[k] for k in ("name", "polarity", "frequency", "calls_seen", "calls_window", "trend",
                                             "severity", "status", "recommended_intervention")} for p in rows}
    label, detail = trajectory(rows)
    titles = {x["node_id"]: x for x in calls}
    obs = [dict(r) for r in conn.execute(
        "SELECT o.*, c.title, c.started_at FROM seller_observations o JOIN calls c ON c.node_id=o.call_id "
        "WHERE o.owner_id=? AND c.owner_id=? ORDER BY c.started_at DESC, o.id DESC LIMIT ?",
        (owner, owner, int(c.get("observations_shown", 40)) + len(gone) * 5))
           if r["tag"] not in gone][:int(c.get("observations_shown", 40))]
    insights, seen = [], set()
    for a in conn.execute("SELECT call_id, json FROM artifacts WHERE kind='analysis' AND owner_id=? ORDER BY id DESC",
                          (owner,)):
        if a["call_id"] in seen or a["call_id"] not in titles:
            continue
        seen.add(a["call_id"])
        data = json.loads(a["json"] or "{}")
        insights.append({"call_id": a["call_id"], "insight": data.get("coaching_insight"),
                         "missed": data.get("biggest_missed_opportunity"), "verdict": data.get("verdict")})
        if len(insights) >= int(c.get("insights_shown", 8)):
            break
    prio = patterns.active_priority(conn)
    return {"calls": calls, "titles": titles, "patterns": rows, "snapshot": snapshot, "trajectory": label,
            "trajectory_detail": detail, "observations": obs, "insights": insights, "nudges": _nudges(conn),
            "default_priority": prio["tag"] if prio else None}


class CoachAgent(IntelAgent):
    name = "longitudinal_coach"
    schema = CoachReport

    def input_refs(self, ctx):
        return {"calls_analysed": len(ctx["calls"])}

    def build_prompt(self, ctx):
        lines = [f"CALLS ANALYSED: {len(ctx['calls'])} (newest first)"]
        lines += [f"- {c['node_id']} | {c['title'] or '(untitled)'} | {(c['started_at'] or '')[:10]}" for c in ctx["calls"][:20]]
        lines += ["", "SELLER PATTERNS (computed by code from every analysed call; quote these numbers only as given, "
                      "never compute new ones)"]
        for p in ctx["patterns"]:
            lines.append(f"- {p['tag']} | {p['name']} | {p['polarity']} | {p['status']} | seen on {p['calls_seen']} calls"
                         f" | in window {round(p['frequency'] * 100)}% of the last {p['calls_window']} | trend {p['trend']}"
                         f" | severity {p['severity']}")
        lines += ["", f"TRAJECTORY (decided by code from the trend column): {ctx['trajectory']}",
                  f"Trend counts: {json.dumps(ctx['trajectory_detail'])}",
                  f"Default coaching priority (most frequent active weakness): {ctx['default_priority'] or 'none'}", "",
                  "RECENT OBSERVATIONS (verbatim quotes, with call and turns)"]
        for o in ctx["observations"]:
            lines.append(f"- {o['call_id']} turns {o['evidence_turns']} | {o['tag']} ({o['polarity']}, {o['severity']}, "
                         f"{o['confidence']}) | contexts {o['contexts']} | \"{o['evidence_quote'] or ''}\"")
        lines += ["", "COACHING INSIGHTS FROM EACH CALL (newest first)"]
        for i in ctx["insights"]:
            ins, v = i["insight"] or {}, i["verdict"] or {}
            lines.append(f"- {i['call_id']} | verdict {v.get('label', '-')}: {v.get('one_line', '')} | insight: "
                         f"{ins.get('insight', '-')} (turns {ins.get('evidence_turns', [])}) | practise: "
                         f"{ins.get('practice_next_call', '-')}")
            m = i["missed"]
            if m:
                lines.append(f"  missed (turns {m['evidence_turns']}): {m['what_happened']} | instead: "
                             f"{m['what_to_do_instead']} | words: {m['suggested_words']}")
        n = ctx["nudges"]
        lines += ["", "LIVE NUDGES DURING CALLS"]
        if n is None or not n["rows"]:
            lines.append("(live coaching has not shown any nudges yet)")
        else:
            lines.append(f"Nudges shown on live calls (latest {n['shown']})")
            if n["outcome_col"]:
                lines.append(f"Outcome counts ({n['outcome_col']}): {json.dumps(n['counts'])}")
            if n["adherence_pct"] is not None:
                lines.append(f"Followed {n['adherence_pct']}% of the nudges with a known outcome (computed by code)")
            for r in n["rows"][:20]:
                lines.append("- " + " | ".join(f"{k}={str(v)[:120]}" for k, v in r.items() if v not in (None, "")))
        return "\n".join(lines)


def _clean_numbers(text, allowed: set, notes: list) -> str:
    def repl(m):
        nums = [int(g) for g in (m.group(1), m.group(3), m.group(4)) if g]
        if all(n in allowed for n in nums):
            return m.group(0)
        notes.append(f"removed a number not in the pattern data: {m.group(0)!r}")
        return "[number removed]"
    return _NUMBER.sub(repl, voice_lint.autofix(text or ""))


def validate(conn, ctx, raw: dict) -> dict:
    notes = []
    snap = ctx["snapshot"]
    allowed = {len(ctx["calls"])}
    for p in snap.values():
        allowed |= {round(p["frequency"] * 100), p["calls_seen"], p["calls_window"]}
    if ctx["nudges"]:
        allowed |= {v for v in ctx["nudges"]["counts"].values() if isinstance(v, int)}
        allowed |= {v for v in (ctx["nudges"]["shown"], ctx["nudges"]["adherence_pct"]) if isinstance(v, int)}
    call_ids = {c["node_id"] for c in ctx["calls"]}
    turn_cache = {}

    def turns_of(cid):
        if cid not in turn_cache:
            turn_cache[cid] = {t["idx"]: dict(t) for t in repo.turns(conn, cid, "final")}
        return turn_cache[cid]

    def refs(items):
        out = []
        for r in items:
            r = dict(r)
            if r["call_id"] not in call_ids:
                notes.append(f"dropped a reference to {r['call_id']}: not an analysed call")
                continue
            if (r.get("quote") or "").strip():
                check = ev.check(r["quote"], r["turns"], turns_of(r["call_id"]))
                r["verified"] = check.found
                if not check.found:
                    notes.append(f"quote not found in {r['call_id']} turns {r['turns']}; kept the reference, dropped the quote")
                    r["quote"] = ""
            out.append(r)
        return out

    def pattern_notes(items, polarity):
        out = []
        for p in items:
            info = snap.get(p["tag"])
            if info is None or info["polarity"] != polarity:
                notes.append(f"dropped {polarity} note on {p['tag']!r}: not a {polarity} pattern in the data")
                continue
            out.append({"tag": p["tag"], "summary": _clean_numbers(p["summary"], allowed, notes),
                        "evidence": refs(p["evidence"]), "pattern": info})
        return out

    active = {t for t, p in snap.items() if p["polarity"] == "weakness" and p["status"] == "active"}
    prio = raw["priority_tag"]
    if prio not in active:
        fallback = ctx["default_priority"] or next((t for t, p in snap.items() if p["polarity"] == "weakness"), None)
        if prio != fallback:
            notes.append(f"priority {prio!r} is not an active weakness; used {fallback!r}")
        prio = fallback
    well = []
    for w in raw["well_handled"]:
        if w["call_id"] not in call_ids:
            notes.append(f"dropped a well-handled call {w['call_id']}: not an analysed call")
            continue
        well.append({"call_id": w["call_id"], "why": _clean_numbers(w["why"], allowed, notes),
                     "evidence": refs(w["evidence"])})
    say = []
    for s in raw["say_differently"]:
        inst = refs([s["instead_of"]])
        say.append({"situation": _clean_numbers(s["situation"], allowed, notes),
                    "instead_of": inst[0] if inst else None, "say": voice_lint.autofix(s["say"])})
    return {
        "headline": _clean_numbers(raw["headline"], allowed, notes),
        "strengths": pattern_notes(raw["strengths"], "strength"),
        "weaknesses": pattern_notes(raw["weaknesses"], "weakness"),
        "trajectory": {"label": ctx["trajectory"], "detail": ctx["trajectory_detail"],
                       "explanation": _clean_numbers(raw["trajectory"], allowed, notes)},
        "priority": {"tag": prio, "pattern": snap.get(prio) if prio else None,
                     "why": _clean_numbers(raw["priority_why"], allowed, notes),
                     "practice": _clean_numbers(raw["practice"], allowed, notes)},
        "well_handled": well, "say_differently": say,
        "patterns_snapshot": snap, "calls_analysed": len(ctx["calls"]),
        "nudges": {k: ctx["nudges"][k] for k in ("outcome_col", "counts", "shown", "adherence_pct")}
        if ctx["nudges"] and ctx["nudges"]["rows"] else None,
        "notes": notes,
    }


def _owner(conn) -> str:
    """The acting user, or the rep whose Coach page a manager is reading (identity.viewing, read-only)."""
    from .. import identity
    return identity.subject_id(conn)


def latest(conn) -> dict | None:
    row = conn.execute("SELECT * FROM coach_reports WHERE owner_id=? ORDER BY id DESC LIMIT 1", (_owner(conn),)).fetchone()
    if row is None:
        return None
    report = json.loads(row["json"])
    report.update(id=row["id"], created_at=row["created_at"], run_id=row["run_id"], trigger=row["trigger"])
    return report


def refresh(conn, trigger="manual", force=False) -> int | None:
    """Regenerate the report. Automatic triggers need coach.min_calls analysed calls; manual needs one.
    Returns the new report id, or None when skipped, cached or failed (the failure is kept in state)."""
    ctx = build(conn)
    need = 1 if trigger == "manual" else int(_cfg().get("min_calls", 3))
    if len(ctx["calls"]) < need:
        return None
    agent = CoachAgent()
    _, input_sha = shas(agent.system_prompt(ctx), agent.build_prompt(ctx))
    last = conn.execute("SELECT input_sha FROM coach_reports WHERE owner_id=? ORDER BY id DESC LIMIT 1",
                        (_owner(conn),)).fetchone()
    if not force and last and last["input_sha"] == input_sha:
        return None
    try:
        out, run_id, _, input_sha = agent.run(conn, ctx)
    except AgentFailed as exc:
        log.warning("coach report failed: %s", exc)
        stores.set_user_state(conn, "intel:coach_error", f"{stores.now()} {str(exc)[:500]}")
        conn.commit()
        if exc.rate_limited:          # an on-demand request is deferred by the worker; auto triggers swallow it
            raise
        return None
    report = validate(conn, ctx, out.model_dump())
    cur = conn.execute("INSERT INTO coach_reports(calls_analysed,trigger,input_sha,run_id,json,created_at,owner_id) "
                       "VALUES (?,?,?,?,?,?,?)", (len(ctx["calls"]), trigger, input_sha, run_id,
                                                  json.dumps(report), stores.now(), _owner(conn)))
    stores.set_user_state(conn, "intel:coach_error", "")
    conn.commit()
    return db.insert_id(cur)
