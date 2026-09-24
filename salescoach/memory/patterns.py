"""Seller-pattern memory: deterministic aggregation over per-call observations.

The analyst agent only reports what it saw on ONE call (seller_observations).
Frequency, trend, first/last detected are computed here, by counting, so the
numbers in the coaching view are never a model's guess.

  frequency  = calls in the window with the tag / calls analysed in the window
  trend      = frequency over the last 5 analysed calls vs the 5 before
               (needs 6+ analysed calls; for a strength, rising is improving)
  status     = candidate until seen on 2 distinct calls, then active; retired when the user said
               "Wrong" or "Retire" on the learning page (review 3: the verdict there governs here too)

The learning layer's verdicts (learned_patterns.user_state / no_prompt, phase F) are honoured by
every reader of THIS table that feeds a prompt or a page: suppressed_tags() is the one list, and
active_priority() (the prep brief, the Today card, the coach report's default) skips it.
"""
import json
import re
from collections import Counter

from .. import config, identity
from ..store.stores import now

WINDOW = 10
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def taxonomy() -> dict:
    return {p["tag"]: p for p in (config.load("seller_taxonomy").get("patterns") or [])}


def normalize_tag(tag: str, known: dict) -> str:
    tag = (tag or "").strip()
    if tag in known or tag.startswith("new:"):
        return tag
    slug = re.sub(r"[^a-z0-9]+", "_", tag.lower()).strip("_") or "unnamed"
    return f"new:{slug}"


def record_observations(conn, call_id: str, observations) -> int:
    known = taxonomy()
    conn.execute("DELETE FROM seller_observations WHERE call_id=?", (call_id,))
    for ob in observations:
        conn.execute(
            "INSERT INTO seller_observations(call_id,tag,polarity,severity,contexts,evidence_turns,"
            "evidence_quote,confidence,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (call_id, normalize_tag(ob.tag, known), ob.polarity, ob.severity, json.dumps(ob.contexts),
             json.dumps(ob.evidence_turns), ob.evidence_quote, ob.confidence, now()))
    return len(observations)


def _learned_verdicts(conn) -> dict:
    """{tag: user_state | 'no_prompt'} from the learning layer, for seller patterns the user judged.
    Empty when the learning tables are not there (plugins off) or predate the no_prompt column."""
    out = {}
    # Asked, not tried: a failed statement would abort an open Postgres transaction.
    if not conn.table_exists("learned_patterns"):
        return out
    if "no_prompt" in conn.columns("learned_patterns"):
        sql = ("SELECT key, user_state, no_prompt FROM learned_patterns WHERE family='seller' "
               "AND (user_state IN ('wrong','retired') OR no_prompt=1)")
    else:
        sql = ("SELECT key, user_state, 0 AS no_prompt FROM learned_patterns WHERE family='seller' "
               "AND user_state IN ('wrong','retired')")
    for r in conn.execute(sql + " AND owner_id=?", (_owner(conn),)):
        out[r["key"]] = r["user_state"] if r["user_state"] in ("wrong", "retired") else "no_prompt"
    return out


def _owner(conn) -> str:
    return identity.actor_of(conn).user_id


def retired_tags(conn) -> set:
    """Tags the user marked Wrong or Retired: seller_patterns.status mirrors them as 'retired'."""
    return {tag for tag, verdict in _learned_verdicts(conn).items() if verdict in ("wrong", "retired")}


def suppressed_tags(conn) -> set:
    """Tags no prompt and no priority card may carry: Wrong, Retired, or "do not use in prompts"."""
    return set(_learned_verdicts(conn))


def _analysed_calls(conn):
    """The acting user's analysed calls, newest first (the seller memory is one user's)."""
    return [r["node_id"] for r in conn.execute(
        "SELECT c.node_id FROM calls c WHERE c.owner_id=? AND EXISTS "
        "(SELECT 1 FROM artifacts a WHERE a.call_id=c.node_id AND a.kind='analysis') "
        "ORDER BY c.started_at DESC", (_owner(conn),))]


def recompute(conn, window: int = WINDOW):
    """Rebuild the ACTING user's seller_patterns from their own observations. Two users get two rows
    per tag (PRIMARY KEY (owner_id, tag)); nothing here reads another user's calls."""
    known = taxonomy()
    owner = _owner(conn)
    analysed = _analysed_calls(conn)
    in_window = analysed[:window]
    retired = retired_tags(conn)
    tags = {r["tag"] for r in conn.execute("SELECT DISTINCT tag FROM seller_observations WHERE owner_id=?", (owner,))}
    for tag in tags:
        obs = conn.execute(
            "SELECT o.*, c.started_at FROM seller_observations o JOIN calls c ON c.node_id=o.call_id "
            "WHERE o.tag=? AND o.owner_id=? AND o.confidence != 'low' ORDER BY c.started_at", (tag, owner)).fetchall()
        if not obs:
            continue
        calls_with = {o["call_id"] for o in obs}
        seen_window = [c for c in in_window if c in calls_with]
        frequency = len(seen_window) / len(in_window) if in_window else 0.0
        polarity = obs[-1]["polarity"]
        trend = _trend(analysed, calls_with, polarity)
        recent = [o for o in obs if o["call_id"] in in_window] or obs
        severity = max((o["severity"] for o in recent), key=lambda s: SEVERITY_ORDER[s])
        contexts = Counter(c for o in recent for c in json.loads(o["contexts"] or "[]"))
        examples = [{"call_id": o["call_id"], "quote": o["evidence_quote"],
                     "turns": json.loads(o["evidence_turns"] or "[]")} for o in obs[-3:]][::-1]
        entry = known.get(tag, {})
        name = entry.get("name") or tag.removeprefix("new:").replace("_", " ").capitalize()
        status = "retired" if tag in retired else ("active" if len(calls_with) >= 2 else "candidate")
        conn.execute(
            "INSERT INTO seller_patterns(tag,name,description,polarity,frequency,calls_seen,calls_window,severity,"
            "contexts,examples,first_detected,last_detected,trend,recommended_intervention,status,updated_at,owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,tag) DO UPDATE SET "
            "name=excluded.name, description=excluded.description, polarity=excluded.polarity, "
            "frequency=excluded.frequency, calls_seen=excluded.calls_seen, calls_window=excluded.calls_window, "
            "severity=excluded.severity, contexts=excluded.contexts, examples=excluded.examples, "
            "first_detected=excluded.first_detected, last_detected=excluded.last_detected, trend=excluded.trend, "
            "recommended_intervention=excluded.recommended_intervention, status=excluded.status, "
            "updated_at=excluded.updated_at",
            (tag, name, entry.get("description") or entry.get("name"), polarity, round(frequency, 3),
             len(calls_with), len(in_window), severity, json.dumps([c for c, _ in contexts.most_common(3)]),
             json.dumps(examples), obs[0]["started_at"], obs[-1]["started_at"], trend,
             entry.get("recommended_intervention"), status, now(), owner))


def _trend(analysed, calls_with, polarity) -> str:
    if len(analysed) < 6:
        return "insufficient_data"
    last, prev = analysed[:5], analysed[5:10]
    f_last = sum(c in calls_with for c in last) / len(last)
    f_prev = sum(c in calls_with for c in prev) / len(prev)
    delta = f_last - f_prev
    if abs(delta) < 0.2:
        return "stable"
    rising = delta > 0
    good = rising if polarity == "strength" else not rising
    return "improving" if good else "worsening"


def active_priority(conn):
    """The single weakness to work on next: most frequent active weakness, severity breaking ties."""
    gone = suppressed_tags(conn)
    rows = conn.execute(
        "SELECT * FROM seller_patterns WHERE owner_id=? AND polarity='weakness' AND status='active' "
        "ORDER BY frequency DESC, CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END", (_owner(conn),)
    ).fetchall()
    return next((r for r in rows if r["tag"] not in gone), None)
