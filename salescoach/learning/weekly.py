"""The weekly "what changed": a structured dict and a plain-text rendering of it.

    digest(conn, since)  -> dict        since: ISO string, date or datetime; d["text"] is the rendering
    render_text(d)       -> str
    last_days(conn, n=7) -> dict

(also reachable as salescoach.learning.digest and salescoach.plugins.learning.digest)

Every number is counted here, from tables the learning layer already fills:

  new_active / dormant / returned   the `learned_pattern_status` events patterns.recompute emits when a
                                    pattern's status or label changes (net change over the window)
  open_proposals / decided          learning_proposals
  outcomes                          derived_outcomes rows (re)written in the window, per kind, by value
  deals                             deal_stage_history rows that closed a deal, with the seller's reason

Text comes from pattern summaries (taxonomy names, fixed rule sentences, counts), proposal summaries
(tag names and counts), deal names and the seller's own lost reason. Nothing from an email or a
transcript is read here, so nothing from one can be in it. Nothing is sent anywhere: the Learning page
shows it and `salescoach learn --digest` prints it.
"""
import json
from datetime import datetime, timedelta, timezone

from ..automation import common
from ..store.stores import now
from . import outcomes, patterns


def _since(value) -> datetime:
    at = common.ts(value)
    if at is None:
        raise ValueError(f"not a date or timestamp: {value!r}")
    return at


def _in_window(raw, since: datetime) -> bool:
    at = common.ts(raw)
    return at is not None and at >= since


def _pattern_changes(conn, since: datetime) -> dict:
    net: dict[str, dict] = {}
    for ev in conn.execute("SELECT ts, node_id, before, after FROM events WHERE kind='learned_pattern_status' ORDER BY id"):
        if not _in_window(ev["ts"], since):
            continue
        before, after = json.loads(ev["before"] or "{}"), json.loads(ev["after"] or "{}")
        entry = net.setdefault(ev["node_id"], {"first": before, "last": after, "returned": False})
        entry["last"] = after
        if before.get("status") in ("dormant", "retired") and after.get("status") in ("candidate", "active"):
            entry["returned"] = True
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM learned_patterns WHERE owner_id=?", (patterns._owner(conn),))}
    out = {"new_active": [], "dormant": [], "returned": []}
    for pid in sorted(net):
        entry, row = net[pid], rows.get(pid)
        if row is None:
            continue
        item = {"id": pid, "family": row["family"], "family_title": patterns.FAMILY_LABELS.get(row["family"], row["family"]),
                "summary": row["summary"], "label": row["label"], "status": row["status"]}
        if entry["returned"] and row["status"] in ("candidate", "active"):
            out["returned"].append(item)
        elif row["status"] == "active" and entry["first"].get("status") != "active":
            out["new_active"].append(item)
        if row["status"] == "dormant" and entry["first"].get("status") != "dormant":
            out["dormant"].append(item)
    return out


def _outcomes_moved(conn, since: datetime) -> list[dict]:
    moved = {k: {"kind": k, "yes": 0, "no": 0, "pending": 0} for k in outcomes.KINDS}
    for r in conn.execute("SELECT kind, value, computed_at FROM derived_outcomes"):
        if r["kind"] in moved and _in_window(r["computed_at"], since):
            moved[r["kind"]][{1: "yes", 0: "no"}.get(r["value"], "pending")] += 1
    totals = {c["kind"]: c for c in outcomes.counts(conn)}
    return [{**m, "total": totals.get(k, {}).get("n", 0)} for k, m in moved.items() if m["yes"] or m["no"] or m["pending"]]


def _deals_closed(conn, since: datetime) -> list[dict]:
    reasons, out = outcomes.lost_reasons(), []
    for h in conn.execute("SELECT h.*, d.name FROM deal_stage_history h LEFT JOIN deals d ON d.node_id=h.deal_id "
                          "WHERE h.to_status IN ('won','lost') AND h.from_status IS NOT h.to_status ORDER BY h.id"):
        if not _in_window(h["changed_at"], since):
            continue
        code, text = outcomes.lost_reason_parts(h["lost_reason"])
        out.append({"deal_id": h["deal_id"], "name": h["name"] or h["deal_id"], "status": h["to_status"],
                    "reason": reasons.get(code, code) if h["to_status"] == "lost" else "",
                    "reason_text": text if h["to_status"] == "lost" else "", "at": h["changed_at"]})
    return out


def digest(conn, since) -> dict:
    start = _since(since)
    changes = _pattern_changes(conn, start)
    decided = [{"id": p["id"], "kind": p["kind"], "status": p["status"], "summary": p["summary"]}
               for p in patterns.decided_proposals(conn) if _in_window(p["resolved_at"], start)]
    d = {"since": start.isoformat(timespec="seconds"), "until": now(), **changes,
         "open_proposals": [{"id": p["id"], "kind": p["kind"], "summary": p["summary"]}
                            for p in patterns.open_proposals(conn)],
         "decided_proposals": decided,
         "outcomes": _outcomes_moved(conn, start), "deals": _deals_closed(conn, start)}
    d["empty"] = not any(d[k] for k in ("new_active", "dormant", "returned", "open_proposals", "decided_proposals",
                                        "outcomes", "deals"))
    d["text"] = render_text(d)
    return d


def last_days(conn, days: int = 7) -> dict:
    days = max(1, int(days))
    d = digest(conn, datetime.now(timezone.utc) - timedelta(days=days))
    d["days"] = days
    d["text"] = render_text(d)
    return d


def _pattern_line(p) -> str:
    return f"  - {p['family_title']}: {p['summary']}" + (f" [{p['label']}]" if p["label"] else "") + f"  ({p['id']})"


def render_text(d: dict) -> str:
    span = f"the last {d['days']} days" if d.get("days") else f"since {d['since'][:10]}"
    lines = [f"What changed in {span} (since {d['since'][:10]})"]
    if d["empty"]:
        return "\n".join(lines + ["  Nothing changed."])
    for key, title in (("new_active", "New active patterns"), ("returned", "Returned after going quiet"),
                       ("dormant", "Went dormant")):
        if d[key]:
            lines.append(f"{title} ({len(d[key])})")
            lines += [_pattern_line(p) for p in d[key]]
    if d["open_proposals"]:
        lines.append(f"Open proposals ({len(d['open_proposals'])}), accept or dismiss on /learning")
        lines += [f"  - #{p['id']} {p['summary']}" for p in d["open_proposals"]]
    if d["decided_proposals"]:
        lines.append(f"Proposals you decided ({len(d['decided_proposals'])})")
        lines += [f"  - #{p['id']} {p['status']}: {p['summary']}" for p in d["decided_proposals"]]
    if d["outcomes"]:
        lines.append("Outcomes that moved")
        lines += [f"  - {o['kind'].replace('_', ' ')}: {o['yes']} yes, {o['no']} no, {o['pending']} still open "
                  f"(of {o['total']} tracked)" for o in d["outcomes"]]
    if d["deals"]:
        lines.append(f"Deals closed ({len(d['deals'])})")
        for x in d["deals"]:
            why = ": ".join(t for t in (x["reason"], x["reason_text"]) if t)
            lines.append(f"  - {x['name']}: {x['status']}" + (f" ({why})" if why else ""))
    return "\n".join(lines)
