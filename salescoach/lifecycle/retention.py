"""Retention: calls older than the org's `retention.days` are deleted, with everything that hangs off them.

Who runs it. The scheduler's `retention` duty (automation/scheduler.py): in the split deploy only the scheduler
role's leader runs duties; the duty is per owner, so each round runs it once for every active user inside that
user's SERVICE session, and every statement is that owner's own delete under row-level security (a duty for rep A
can touch nothing of rep B's). The period itself is the org's (config/org.yaml, Settings). `salescoach retention
--dry-run` prints what a round would delete; without --dry-run it runs a round now.

What goes, per expired call (owner-scoped, in batches of `retention.batch` calls, one transaction and one
`retention.purge` audit event per batch):
  the call and its node, turns, speakers, participants, artifacts, claims, assessments, agent_runs of the call,
  seller observations, live-coach nudges and state, embeddings, pattern observations, derived outcomes, the
  node's edges, events and source row, the raw payload the call came from; loops FROM the call that are closed
  (done, cancelled, superseded: with their decisions, provenance, conflicts, outcomes, events and nodes);
  emails from the call that are not 'sending' (with their edits, slot fills, auto-send log, the replies to them
  and those replies' proposals); the comments and access-log rows about all of these (Postgres: through
  app_forget_annotations, store/rls.py, since a manager's comment is deletable by its author only).
  What was derived from them: the seller memory is recomputed from the observations that are left (a pattern
  with none left goes, with the verbatim quotes and call ids in its `examples`), and every coach report that
  cites a deleted call is deleted (a report is a model's narrative over the calls; it cannot be recomputed
  without a model call, so it goes, and the next analysed call or a refresh on the Coach page writes a new one).
What stays: deals, accounts and people (never deleted); open loops and 'sending' emails of an expired call
(their call_id is cleared); deal-level history (deal health, stage history, prep briefs). A call that is live
or has a pending or running bus event is left for the next round.
Not reached: a disabled user's calls (row-level security gives a disabled user's session nothing); offboard
them (reassign or purge, lifecycle/offboard.py) and their work follows the new owner's retention, or is gone.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import identity
from ..store.stores import now
from . import settings

CLOSED_LOOPS = ("done", "cancelled", "superseded")


def cutoff_for(days: int, at: Optional[datetime] = None) -> str:
    """The first date that is kept, 'YYYY-MM-DD' (UTC). A call started before it is expired. Stored times
    are ISO text with an offset, so a string comparison with the date is right to within the offset: the
    retention period is in days, a call is at most hours early or late."""
    moment = at or datetime.now(timezone.utc)
    return (moment - timedelta(days=days)).date().isoformat()


def _owner(conn) -> str:
    return identity.actor_of(conn).user_id


def _marks(values) -> str:
    return ",".join("?" * len(values))


def expired_calls(conn, cutoff: str, limit: int) -> list:
    """The acting owner's calls started before `cutoff` that nothing is working on, oldest first."""
    return [r["node_id"] for r in conn.execute(
        "SELECT c.node_id FROM calls c WHERE c.owner_id=? AND COALESCE(c.started_at, c.updated_at) IS NOT NULL "
        "AND COALESCE(c.started_at, c.updated_at) < ? AND c.wf_state != 'live' "
        "AND NOT EXISTS (SELECT 1 FROM wf_events e WHERE e.entity_id = c.node_id AND e.status IN ('pending','running')) "
        "ORDER BY COALESCE(c.started_at, c.updated_at), c.node_id LIMIT ?", (_owner(conn), cutoff, limit)).fetchall()]


def count_expired(conn, cutoff: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM calls c WHERE c.owner_id=? AND COALESCE(c.started_at, c.updated_at) IS NOT NULL "
        "AND COALESCE(c.started_at, c.updated_at) < ? AND c.wf_state != 'live'", (_owner(conn), cutoff)).fetchone()[0]


def _ids(conn, sql, params) -> list:
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def _delete(conn, counts, table, where, params, key=None):
    cur = conn.execute(f"DELETE FROM {table} WHERE owner_id=? AND {where}", (_owner(conn), *params))
    if cur.rowcount:
        counts[key or table] = counts.get(key or table, 0) + cur.rowcount


def _forget_annotations(conn, counts, entity_type, ids):
    """Comments and access-log rows about objects being deleted, whoever wrote them."""
    if not ids:
        return
    ids = [str(i) for i in ids]
    if conn.dialect == "postgres":
        n = conn.execute("SELECT app_forget_annotations(?, ?)", (entity_type, ids)).fetchone()[0]
    else:
        n = conn.execute(f"DELETE FROM comments WHERE owner_id=? AND entity_type=? AND entity_id IN ({_marks(ids)})",
                         (_owner(conn), entity_type, *ids)).rowcount
        if entity_type in ("call", "deal", "email"):
            n += conn.execute(f"DELETE FROM access_log WHERE owner_user_id=? AND entity_type=? "
                              f"AND entity_id IN ({_marks(ids)})", (_owner(conn), entity_type, *ids)).rowcount
    if n:
        counts["comments_and_views"] = counts.get("comments_and_views", 0) + n


def _delete_nodes(conn, counts, ids):
    """A node and what the engine keeps about it: edges either way, events, its source row."""
    m = _marks(ids)
    _delete(conn, counts, "edges", f"(src IN ({m}) OR dst IN ({m}))", (*ids, *ids))
    _delete(conn, counts, "events", f"node_id IN ({m})", ids)
    _delete(conn, counts, "sources", f"node_id IN ({m})", ids)


def delete_calls(conn, call_ids: list) -> dict:
    """Delete `call_ids` (the acting owner's) and what hangs off them. Does not commit. Returns counts."""
    counts: dict = {}
    if not call_ids:
        return counts
    me, m = _owner(conn), _marks(call_ids)
    # loops from the calls: closed ones go, open ones stay without their call
    loops = _ids(conn, f"SELECT node_id FROM loops WHERE owner_id=? AND call_id IN ({m}) AND status IN "
                       f"({_marks(CLOSED_LOOPS)})", (me, *call_ids, *CLOSED_LOOPS))
    conn.execute(f"UPDATE loops SET call_id=NULL WHERE owner_id=? AND call_id IN ({m})", (me, *call_ids))
    # emails from the calls: all but 'sending' go; a 'sending' one stays (its outcome is still unknown)
    emails = _ids(conn, f"SELECT id FROM emails WHERE owner_id=? AND call_id IN ({m}) AND status != 'sending'",
                  (me, *call_ids))
    conn.execute(f"UPDATE emails SET call_id=NULL WHERE owner_id=? AND call_id IN ({m})", (me, *call_ids))
    if emails:
        e = _marks(emails)
        replies = _ids(conn, f"SELECT id FROM email_replies WHERE owner_id=? AND email_id IN ({e})", (me, *emails))
        if replies:
            _delete(conn, counts, "reply_proposals", f"reply_id IN ({_marks(replies)})", replies)
            _delete(conn, counts, "email_replies", f"id IN ({_marks(replies)})", replies)
        for table in ("email_edits", "slot_fills", "autosend_log", "pattern_observations"):
            _delete(conn, counts, table, f"email_id IN ({e})", emails)
        conn.execute(f"UPDATE followup_decisions SET email_id=NULL WHERE owner_id=? AND email_id IN ({e})", (me, *emails))
        _delete(conn, counts, "derived_outcomes", f"subject_type='email' AND subject_id IN ({e})", [str(i) for i in emails])
        _forget_annotations(conn, counts, "email", emails)
        _delete(conn, counts, "emails", f"id IN ({e})", emails)
    if loops:
        lm = _marks(loops)
        _delete(conn, counts, "followup_decisions", f"loop_id IN ({lm})", loops)
        _delete(conn, counts, "field_provenance", f"entity_id IN ({lm})", loops)
        _delete(conn, counts, "memory_conflicts", f"entity_id IN ({lm})", loops)
        _delete(conn, counts, "derived_outcomes", f"subject_type='loop' AND subject_id IN ({lm})", loops)
        _forget_annotations(conn, counts, "loop", loops)
        _delete_nodes(conn, counts, loops)
        _delete(conn, counts, "loops", f"node_id IN ({lm})", loops)
        _delete(conn, counts, "nodes", f"id IN ({lm})", loops, key="loop_nodes")
    refs = _ids(conn, f"SELECT source_ref FROM calls WHERE owner_id=? AND node_id IN ({m}) AND source_ref IS NOT NULL",
                (me, *call_ids))
    if refs:
        _delete(conn, counts, "raw_payloads", f"source_ref IN ({_marks(refs)})", refs)
    for table in ("turns", "speakers", "call_participants", "artifacts", "claims", "assessments",
                  "seller_observations", "nudges", "coach_state", "embeddings", "pattern_observations"):
        _delete(conn, counts, table, f"call_id IN ({m})", call_ids)
    _delete(conn, counts, "agent_runs", f"call_id IN ({m})", call_ids)          # after artifacts (run_id)
    _delete(conn, counts, "derived_outcomes", f"subject_type='call' AND subject_id IN ({m})", call_ids)
    conn.execute(f"UPDATE calendar_meetings SET call_id=NULL WHERE owner_id=? AND call_id IN ({m})", (me, *call_ids))
    _forget_annotations(conn, counts, "call", call_ids)
    _delete_nodes(conn, counts, call_ids)
    _delete(conn, counts, "calls", f"node_id IN ({m})", call_ids, key="call_rows")
    _delete(conn, counts, "nodes", f"id IN ({m})", call_ids, key="call_nodes")
    _forget_derived(conn, counts, call_ids)
    return counts


def _cited_calls(value, out: set) -> set:
    """Every "call_id" anywhere in a coach report's JSON (evidence, well-handled calls, say-differently)."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "call_id" and isinstance(v, str):
                out.add(v)
            else:
                _cited_calls(v, out)
    elif isinstance(value, list):
        for v in value:
            _cited_calls(v, out)
    return out


def _forget_derived(conn, counts, call_ids):
    """What the owner's coaching memory holds FROM the deleted calls: seller_patterns examples (verbatim
    quotes with the call id) and coach reports that cite them."""
    from ..memory import patterns
    me, gone = _owner(conn), set(call_ids)
    before = conn.execute("SELECT COUNT(*) FROM seller_patterns WHERE owner_id=?", (me,)).fetchone()[0]
    patterns.recompute(conn)                         # rebuilt from what is left; a pattern with nothing left goes
    after = conn.execute("SELECT COUNT(*) FROM seller_patterns WHERE owner_id=?", (me,)).fetchone()[0]
    if before - after:
        counts["seller_patterns"] = counts.get("seller_patterns", 0) + before - after
    for row in conn.execute("SELECT tag, examples FROM seller_patterns WHERE owner_id=?", (me,)).fetchall():
        try:                                          # belt and braces: recompute already rebuilt the examples
            examples = json.loads(row["examples"] or "[]")
        except ValueError:
            examples = []
        kept = [e for e in examples if not (isinstance(e, dict) and e.get("call_id") in gone)]
        if kept != examples:
            conn.execute("UPDATE seller_patterns SET examples=? WHERE owner_id=? AND tag=?",
                         (json.dumps(kept), me, row["tag"]))
    reports = []
    rows = conn.execute("SELECT id, json FROM coach_reports WHERE owner_id=?", (me,)).fetchall() \
        if conn.table_exists("coach_reports") else []
    for row in rows:
        try:
            cited = _cited_calls(json.loads(row["json"] or "{}"), set())
        except ValueError:
            cited = set()
        if cited & gone:
            reports.append(row["id"])
    if reports:
        _delete(conn, counts, "coach_reports", f"id IN ({_marks(reports)})", reports)


def _audit(conn, days, cutoff, call_ids, counts) -> None:
    conn.execute("INSERT INTO events(ts, actor, kind, node_id, before, after) VALUES (?,?,?,NULL,NULL,?)",
                 (now(), "retention", "retention.purge",
                  json.dumps({"days": days, "cutoff": cutoff, "calls": len(call_ids), "call_ids": call_ids,
                              "counts": counts}, sort_keys=True)))


def purge(conn, days: Optional[int] = None, dry_run: bool = False, batch: Optional[int] = None,
          at: Optional[datetime] = None, max_batches: int = 1000) -> dict:
    """One owner's round (the acting user of `conn`). Commits each batch. dry_run counts and deletes nothing."""
    days = settings.retention_days() if days is None else days
    if not days:
        return {"skipped": "retention.days is not set: everything is kept"}
    cutoff, batch = cutoff_for(days, at), batch or settings.retention_batch()
    if dry_run:
        return {"dry_run": True, "days": days, "cutoff": cutoff, "calls": count_expired(conn, cutoff),
                "held": count_expired(conn, cutoff) - len(expired_calls(conn, cutoff, 10 ** 9))}
    total: dict = {"calls": 0}
    for _ in range(max_batches):
        ids = expired_calls(conn, cutoff, batch)
        if not ids:
            break
        try:
            counts = delete_calls(conn, ids)
            _audit(conn, days, cutoff, ids, counts)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        total["calls"] += len(ids)
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
    return {"days": days, "cutoff": cutoff, **total}


def run_duty(conn) -> dict:
    """The scheduler's per-owner retention duty (automation/scheduler.py)."""
    return purge(conn)
