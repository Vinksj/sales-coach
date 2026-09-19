"""Phase F2: what the coach has learned goes back into the prompts that act on it.

One entry point per question:

  select(conn, target, deal_id=None)   the patterns `target` may see now, already capped and ordered
  voice_block / prep_lines / live_line / priors_block    the text each prompt receives ("" / [] when none)
  ids(items)                           what the run records in agent_runs.input_refs["patterns"]
  live_focus(conn)                     the live coach's one priority habit: pattern, trigger, boost
  used_in(conn)                        per pattern: the targets it feeds now, and the runs that saw it

Rules every target shares (design §7 "Feedback"):
  * only what patterns.for_prompt returns: active, not wrong, not retired, not merged, not flagged
    "do not use in prompts"; and only when config/learning.yaml `feedback.<target>` is on;
  * at most 3 per prompt, each with its id;
  * a rule, a taxonomy name and a coarse count. Never text from a call or an email: email_voice rules
    come from fixed templates and config lists (voice.py), seller/persona/objection summaries from the
    taxonomy and bucket names.

Byte-stability. A fed pattern changes the rendered prompt, and the rendered prompt is the step cache
key (agents/base.py input_sha). That is right when a belief changes and wrong when only a counter
ticks, so nothing here varies with a single new observation:
  * no timestamps, no exact n: the count is shown as a bucket ("seen on 6+ calls") whose steps are
    config `feedback.count_buckets`;
  * selection and order use only the user's confirm, the label, that same bucket, and the id. The exact
    n_calls and last_seen that patterns.for_prompt ranks by are deliberately not used here.
So the block is identical across recomputes until the active set, a label or a bucket changes.

Nothing here may break the step that asks: every read is wrapped, and a failure feeds nothing.
"""
import json
import logging

from .. import seller
from ..memory import patterns as seller_memory
from . import cfg, patterns, voice

log = logging.getLogger("salescoach.learning")

TARGETS = ("prep", "live_coach", "email_drafter", "nudge_drafter", "strategist")
TARGET_LABELS = {"prep": "prep brief", "live_coach": "live coach", "email_drafter": "follow-up email",
                 "nudge_drafter": "nudge email", "strategist": "deal strategist"}
AGENT_TARGET = {"prep_writer": "prep", "live_coach": "live_coach", "email": "email_drafter", "nudge": "nudge_drafter",
                "deal_strategist": "strategist"}
HARD_CAP = 3
MAX_BOOST = 0.3                       # "modest": whatever the config says, one habit never moves a weight further
_LABEL_RANK = {"established": 0, "emerging": 1, "": 2}
_UNITS = {"seller": ("call", "on"), "objection": ("call", "on"), "email_voice": ("edit", "in"),
          "persona": ("deal", "on")}


# ---- settings ---------------------------------------------------------------------------------

def _setting(target: str):
    return (cfg("feedback") or {}).get(target, True)


def enabled(target: str) -> bool:
    """`feedback.<target>: true|false`, or for a target with options `{enabled: true|false, ...}`."""
    value = _setting(target)
    return bool(value.get("enabled", True)) if isinstance(value, dict) else bool(value)


def cap() -> int:
    try:
        return max(0, min(HARD_CAP, int((cfg("feedback") or {}).get("max_patterns", HARD_CAP))))
    except (TypeError, ValueError):
        return HARD_CAP


def boost() -> float:
    value = _setting("live_coach")
    try:
        raw = float(value.get("boost", 0.1)) if isinstance(value, dict) else 0.1
    except (TypeError, ValueError):
        raw = 0.1
    return max(0.0, min(MAX_BOOST, raw))


def live_triggers() -> dict:
    """Seller tag -> live trigger, from config. Only triggers the live coach really has."""
    from ..coach.detectors import TRIGGERS
    return {str(tag): str(trigger) for tag, trigger in ((cfg("feedback") or {}).get("live_triggers") or {}).items()
            if trigger in TRIGGERS}


# ---- coarse counts ------------------------------------------------------------------------------

def _steps() -> list[int]:
    try:
        steps = sorted({int(x) for x in (cfg("feedback") or {}).get("count_buckets") or [] if int(x) >= 1})
    except (TypeError, ValueError):
        steps = []
    return steps or [1, 2, 3, 6, 10, 20, 50, 100]


def bucket(n: int) -> int:
    """The largest configured step at or below n (0 below the first)."""
    return max((s for s in _steps() if s <= int(n or 0)), default=0)


def count_text(n: int, unit: str) -> str:
    """'1 call', '2 calls', '3+ calls', '6+ calls'. Exact only where the next step is the next integer."""
    steps, b = _steps(), bucket(n)
    exact = (b + 1) in steps
    return f"{b}{'' if exact else '+'} {unit}{'' if b == 1 and exact else 's'}"


def _n(row: dict) -> int:
    return {"email_voice": row["n_obs"], "persona": row["n_deals"]}.get(row["family"], row["n_calls"])


def seen_text(row: dict) -> str:
    """'' for a pattern the user confirmed before it was ever counted."""
    if bucket(_n(row)) == 0:
        return ""
    unit, prep = _UNITS.get(row["family"], ("call", "on"))
    text = f"seen {prep} {count_text(_n(row), unit)}"
    if row["family"] in ("seller", "objection"):
        text += f" across {count_text(row['n_deals'], 'deal')}"
    return text


# ---- selection ----------------------------------------------------------------------------------------

def _eligible(conn, target: str, deal_id=None) -> list[dict]:
    """Everything for_prompt allows for the target, in THIS module's stable order."""
    rows = patterns.for_prompt(conn, target, deal_id=deal_id, limit=100_000)
    if not rows:
        return []
    marks = ",".join("?" * len(rows))
    extra = {r["id"]: r for r in conn.execute(
        f"SELECT id, polarity, user_state FROM learned_patterns WHERE id IN ({marks})", [r["id"] for r in rows])}
    taxonomy = seller_memory.taxonomy()
    out = []
    for r in rows:
        more = extra.get(r["id"])
        item = {**r, "polarity": more["polarity"] if more else None,
                "confirmed": bool(more and more["user_state"] == "confirmed")}
        item["seen"] = seen_text(item)
        if r["family"] == "seller":
            item["intervention"] = (taxonomy.get(r["key"]) or {}).get("recommended_intervention")
        if r["family"] == "email_voice":
            item["rule"] = voice.imperative(r["key"])
        out.append(item)
    out.sort(key=lambda p: (not p["confirmed"], _LABEL_RANK.get(p["label"], 2), -bucket(_n(p)), p["id"]))
    return out


def select(conn, target: str, deal_id=None) -> list[dict]:
    """The patterns `target` sees now: [] when the flag is off, when nothing is active, or on any error."""
    if target not in TARGETS or not enabled(target) or cap() <= 0:
        return []
    try:
        rows = _eligible(conn, target, deal_id)
    except Exception:                                   # a learning fault must never fail a draft, a brief or a call
        log.exception("learning feedback for %s failed; feeding nothing", target)
        return []
    if target == "prep":
        weak = [p for p in rows if p["polarity"] == "weakness"]
        strong = [p for p in rows if p["polarity"] == "strength"][:1]
        return (weak[:max(0, cap() - len(strong))] + strong)[:cap()]
    if target == "live_coach":
        mapping = live_triggers()
        for p in rows:                                  # an unmapped tag is skipped without a word
            if p["polarity"] == "weakness" and p["key"] in mapping:
                return [{**p, "trigger": mapping[p["key"]]}]
        return []
    return rows[:cap()]


def ids(items) -> list[str]:
    return [p["id"] for p in items or []]


# ---- the text each target receives -------------------------------------------------------------------------

def _tag(p: dict) -> str:
    return ", ".join(x for x in ("confirmed by you" if p["confirmed"] else "", p["label"], p["seen"]) if x)


def voice_block(items) -> str:
    """Email and nudge drafters. Rules only: the same-deal raw edits stay where they were."""
    if not items:
        return ""
    lines = [f"- [{p['id']}] {p['rule']} ({_tag(p)})" for p in items]
    return (f"HOW {seller.first_name_upper()} WRITES (learned from edits)\n"
            "Rules counted from what was changed before sending, across all deals. They are rules, not text to "
            "reuse. Where one disagrees with the style guide, follow the rule: it is what actually gets sent.\n"
            + "\n".join(lines))


def prep_lines(items) -> list[str]:
    if not items:
        return []
    lines = ["LEARNED SELLING PATTERNS (counted across past calls, not facts about this deal; weaknesses first):"]
    for p in items:
        do = f" | do: {p['intervention']}" if p.get("intervention") and p["polarity"] == "weakness" else ""
        lines.append(f"- [{p['id']}] {p['polarity']}, {_tag(p)}: {p['summary']}{do}")
    return lines


def live_line(focus: dict | None) -> str:
    if not focus:
        return ""
    p = focus["pattern"]
    return (f"Priority habit, learned across {seller.first_name() or 'the seller'}'s past calls [{p['id']}]: "
            f"\"{p['summary']}\" ({_tag(p)}). When it is a close call between two nudges, prefer {focus['trigger']}. "
            "It is a habit to watch for, not evidence about this call.")


def priors_block(items) -> str:
    if not items:
        return ""
    lines = [f"- [{p['id']}] {p['summary']} ({_tag(p)})" for p in items]
    return ("PRIORS FROM PAST DEALS (NOT evidence about this deal)\n"
            "Counted across earlier deals. Use them only to decide what to look for and what to ask next. A prior "
            "is never evidence: do not quote one, do not cite one, and do not set or raise the status of any "
            "element, stakeholder or risk because of one. Every status still needs a quote from THIS deal's calls.\n"
            + "\n".join(lines))


# ---- live coach ----------------------------------------------------------------------------------------------------

def live_focus(conn) -> dict | None:
    """{"pattern", "trigger", "boost"} for the top active weakness that maps to a live trigger, or None."""
    chosen = select(conn, "live_coach")
    if not chosen:
        return None
    return {"pattern": chosen[0], "trigger": chosen[0]["trigger"], "boost": boost()}


# ---- "Used in" ---------------------------------------------------------------------------------------------------------

def runs_seen(conn) -> dict:
    """{pattern_id: {target: runs}} counted from agent_runs.input_refs["patterns"]."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT agent, input_refs FROM agent_runs WHERE input_refs LIKE '%\"patterns\"%'"):
        try:
            seen = json.loads(r["input_refs"] or "{}").get("patterns") or []
        except ValueError:
            continue
        target = AGENT_TARGET.get(r["agent"], r["agent"])
        for pid in seen if isinstance(seen, list) else []:
            per = out.setdefault(str(pid), {})
            per[target] = per.get(target, 0) + 1
    return out


def used_in(conn) -> dict:
    """{pattern_id: {"targets": [...], "runs": n, "by_target": {...}}} for every pattern that feeds a
    prompt now or was seen by a run before. Deal-scoped priors are shown as they apply to any deal."""
    out: dict[str, dict] = {}
    for target in TARGETS:
        for p in select(conn, target):
            out.setdefault(p["id"], {"targets": [], "runs": 0, "by_target": {}})["targets"].append(target)
    for pid, per in runs_seen(conn).items():
        entry = out.setdefault(pid, {"targets": [], "runs": 0, "by_target": {}})
        entry["by_target"], entry["runs"] = per, sum(per.values())
    return out
