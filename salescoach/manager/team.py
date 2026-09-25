"""The /team numbers: one row per rep of every team the viewer manages, and the team's pattern roll-up.

Every number is a count, a median or a mean computed here from the store (SQL, then Python); nothing
is asked of a model. Each is defined by the same predicate as the list it links to (calls.py for calls,
the Loops and Deals pages' own filters for loops and deals), so the number and the list agree.

  calls_week / calls_4w   calls whose started_at date is within the last 7 / 28 days (today included)
  awaiting                calls in awaiting_review with no pipeline error: the rep's review backlog
  overdue_loops           loops open or waiting, not rejected, with a due date before today
  open_deals              deals whose status is active; median_health is the median deal_health score
                          over the open deals that have one (n_scored of them)
  gaps                    the active methodology's elements most often neither known nor partial across
                          the rep's open deals (an element never assessed counts as unknown): top 3
  talk_share              the seller_series talk-share observations (live captures and stereo imports,
                          where the coach saw who spoke), mean of the latest 3 against the 3 before
  emails_drafted / sent   calls within 28 days with a follow-up email drafted / with one sent
  last_activity           the latest call start or email sent

The roll-up (rollup()) counts, per seller-behaviour pattern tag, how many reps have it active and how
many of those have it established. It is read from each rep's own learned_patterns at request time and
stored nowhere, so learning.patterns.for_prompt can never pick it up; a tag is shown only when at least
MIN_REPS reps have it; the email_voice family is never read at all (a rep's email voice is theirs).
"""
import statistics
from datetime import date, timedelta

from . import access, calls

MIN_REPS = 3
ROLLUP_FAMILIES = ("seller",)             # seller-behaviour tags only
NEVER_ROLLED_UP = ("email_voice",)        # a rep's email voice is never blended with anyone's
TALK_WINDOW = 3


def windows(today: date) -> dict:
    return {"today": today.isoformat(), "week_from": (today - timedelta(days=6)).isoformat(),
            "month_from": (today - timedelta(days=27)).isoformat()}


def _grouped(conn, sql: str, ids: list, params=()) -> dict:
    marks = ",".join("?" * len(ids))
    return {r[0]: r for r in conn.execute(sql.format(ids=marks), (*params, *ids)).fetchall()}


def _methodology():
    try:
        from ..intel import methodology
        return methodology.active()
    except Exception:
        return None


def dashboard(conn, today: date) -> dict:
    reps = access.managed_reps(conn)
    w = windows(today)
    ids = [r["id"] for r in reps]
    m = _methodology()
    out = {"reps": [], "windows": w, "methodology": m.name if m else "", "rollup": rollup(conn, ids)}
    if not ids:
        return out

    loops = _grouped(conn, "SELECT owner_id, COUNT(*) FROM loops WHERE status IN ('open','waiting') "
                           "AND review_state!='rejected' AND due_date IS NOT NULL AND due_date<? "
                           "AND owner_id IN ({ids}) GROUP BY owner_id", ids, (w["today"],))
    deals = {}
    for r in conn.execute(f"SELECT d.owner_id, d.node_id, h.score FROM deals d LEFT JOIN deal_health h "
                          f"ON h.deal_id=d.node_id WHERE d.status='active' AND d.owner_id IN ({','.join('?' * len(ids))})",
                          ids).fetchall():
        deals.setdefault(r["owner_id"], []).append((r["node_id"], r["score"]))
    gaps = _gaps(conn, deals, m)
    talk = _talk_share(conn, ids)
    emails = {}
    for key in ("drafted", "sent"):
        for rep in ids:
            emails.setdefault(rep, {})[key] = calls.count(conn, {"rep": rep, "from": w["month_from"], "email": key})
    last_call = _grouped(conn, "SELECT owner_id, MAX(started_at) FROM calls WHERE owner_id IN ({ids}) GROUP BY owner_id", ids)
    last_mail = _grouped(conn, "SELECT owner_id, MAX(sent_at) FROM emails WHERE sent_at IS NOT NULL "
                               "AND owner_id IN ({ids}) GROUP BY owner_id", ids)

    for rep in reps:
        rid = rep["id"]
        scores = [s for _, s in deals.get(rid, []) if s is not None]
        stamps = [x for x in ((last_call.get(rid) or [None, None])[1], (last_mail.get(rid) or [None, None])[1]) if x]
        out["reps"].append({
            **rep,
            "calls_week": calls.count(conn, {"rep": rid, "from": w["week_from"]}),
            "calls_4w": calls.count(conn, {"rep": rid, "from": w["month_from"]}),
            "awaiting": calls.count(conn, {"rep": rid, "state": "awaiting_review"}),
            "overdue_loops": (loops.get(rid) or [None, 0])[1],
            "open_deals": len(deals.get(rid, [])),
            "median_health": statistics.median(scores) if scores else None,
            "n_scored": len(scores),
            "gaps": gaps.get(rid, []),
            "talk_share": talk.get(rid),
            "emails_drafted": emails[rid]["drafted"], "emails_sent": emails[rid]["sent"],
            "last_activity": max(stamps) if stamps else None,
        })
    return out


def _gaps(conn, deals: dict, m, top: int = 3) -> dict:
    """{rep: [{"key", "label", "unknown", "of"}]} over the rep's open deals, most often unknown first."""
    if m is None:
        return {}
    all_deals = [d for rows in deals.values() for d, _ in rows]
    if not all_deals:
        return {}
    known = set()
    for chunk in range(0, len(all_deals), 500):
        part = all_deals[chunk:chunk + 500]
        for r in conn.execute(f"SELECT deal_id, element FROM meddpicc WHERE status IN ('known','partial') "
                              f"AND deal_id IN ({','.join('?' * len(part))})", part).fetchall():
            known.add((r["deal_id"], r["element"]))
    out = {}
    for rep, rows in deals.items():
        counted = []
        for key in m.keys:
            unknown = sum(1 for d, _ in rows if (d, key) not in known)
            if unknown:
                counted.append({"key": key, "label": m.label(key), "unknown": unknown, "of": len(rows)})
        counted.sort(key=lambda g: (-g["unknown"], m.keys.index(g["key"])))
        out[rep] = counted[:top]
    return out


def _talk_share(conn, ids: list) -> dict:
    """{rep: {"latest", "before", "n", "direction"}}; reps with no two-channel call are absent."""
    points: dict = {}
    for r in conn.execute(
            f"SELECT owner_id, value FROM pattern_observations WHERE family='seller_series' AND key='talk_share' "
            f"AND excluded=0 AND value IS NOT NULL AND owner_id IN ({','.join('?' * len(ids))}) "
            f"ORDER BY observed_at, id", ids).fetchall():
        points.setdefault(r["owner_id"], []).append(float(r["value"]))
    out = {}
    for rep, values in points.items():
        latest = values[-TALK_WINDOW:]
        before = values[-2 * TALK_WINDOW:-TALK_WINDOW]
        now_mean = statistics.fmean(latest)
        before_mean = statistics.fmean(before) if before else None
        direction = "flat"
        if before_mean is not None and abs(now_mean - before_mean) >= 0.05:
            direction = "up" if now_mean > before_mean else "down"
        out[rep] = {"latest": now_mean, "before": before_mean, "n": len(values),
                    "direction": direction if before_mean is not None else None}
    return out


def rollup(conn, rep_ids: list) -> dict:
    """{"tags": [{"key", "label", "active", "established"}], "hidden": n, "reps": n, "min_n": MIN_REPS}."""
    result = {"tags": [], "hidden": 0, "reps": len(rep_ids), "min_n": MIN_REPS}
    if not rep_ids:
        return result
    families = [f for f in ROLLUP_FAMILIES if f not in NEVER_ROLLED_UP]
    rows = conn.execute(
        f"SELECT owner_id, key, label FROM learned_patterns WHERE family IN ({','.join('?' * len(families))}) "
        f"AND status='active' AND merged_into IS NULL AND (user_state IS NULL OR user_state<>'wrong') "
        f"AND owner_id IN ({','.join('?' * len(rep_ids))})", (*families, *rep_ids)).fetchall()
    active, established = {}, {}
    for r in rows:
        active.setdefault(r["key"], set()).add(r["owner_id"])
        if r["label"] == "established":
            established.setdefault(r["key"], set()).add(r["owner_id"])
    for key, owners in active.items():
        if len(owners) < MIN_REPS:
            result["hidden"] += 1
            continue
        result["tags"].append({"key": key, "label": _label(key), "active": len(owners),
                               "established": len(established.get(key, ()))})
    result["tags"].sort(key=lambda t: (-t["active"], -t["established"], t["key"]))
    return result


def _label(key: str) -> str:
    return key.removeprefix("new:").replace("_", " ").strip().capitalize()

