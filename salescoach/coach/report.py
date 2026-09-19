"""Reading the nudges table back: sessions, one session's timeline, and the
numbers the tuning loop cares about (how many shown, why the rest were held
back, how often the seller acted on a nudge)."""
from collections import Counter

from .text import mmss


def sessions(conn, call_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT session, mode, MIN(created_at) AS started, COUNT(*) AS candidates, SUM(shown) AS shown "
        "FROM nudges WHERE call_id=? GROUP BY session, mode ORDER BY MIN(id) DESC", (call_id,)).fetchall()
    return [dict(r) for r in rows]


def rows(conn, call_id: str, session: str | None = None) -> tuple[str | None, list[dict]]:
    if session is None:
        latest = sessions(conn, call_id)
        session = latest[0]["session"] if latest else None
    if session is None:
        return None, []
    out = conn.execute("SELECT * FROM nudges WHERE call_id=? AND session=? ORDER BY t_call, id",
                       (call_id, session)).fetchall()
    return session, [dict(r) for r in out]


def summarise(nudges: list[dict]) -> dict:
    shown = [n for n in nudges if n["shown"]]
    suppressed = [n for n in nudges if not n["shown"]]
    outcomes = Counter(n["outcome"] or "pending" for n in shown)
    rated = outcomes["followed"] + outcomes["ignored"]
    return {
        "candidates": len(nudges), "shown": len(shown), "suppressed": len(suppressed),
        "reasons": Counter(n["suppressed_reason"] or "-" for n in suppressed).most_common(),
        "by_trigger": Counter(n["trigger"] for n in nudges).most_common(),
        "outcomes": dict(outcomes),
        "dismissed": sum(1 for n in shown if n["dismissed"]),
        "adherence": round(outcomes["followed"] / rated, 2) if rated else None,
    }


def timeline_text(call: dict, session: str, nudges: list[dict], verbose: bool = False) -> str:
    s = summarise(nudges)
    lines = [f"{call['node_id']}  {call.get('title') or ''}", f"session {session}", "",
             f"SHOWN ({s['shown']})"]
    for n in nudges:
        if n["shown"]:
            lines.append(f"  {mmss(n['t_call'])}  {n['trigger']:<16} {n['source']:<4} score {n['score']:.2f}  "
                         f"\"{n['text']}\"  -> {n['outcome'] or '-'}{' (dismissed)' if n['dismissed'] else ''}")
            if n["anchor_text"]:
                lines.append(f"         anchor {mmss(n['anchor_t'])}: {n['anchor_text'][:150]}")
            if n["rationale"]:
                lines.append(f"         why: {n['rationale'][:200]}")
    lines += ["", f"SUPPRESSED ({s['suppressed']}): " + ", ".join(f"{r} {c}" for r, c in s["reasons"])]
    lines.append("candidates by trigger: " + ", ".join(f"{t} {c}" for t, c in s["by_trigger"]))
    if verbose:
        for n in nudges:
            if not n["shown"]:
                lines.append(f"  {mmss(n['raised_t'])}  {n['trigger']:<16} {n['source']:<4} "
                             f"{(n['score'] or 0):.2f}  {n['suppressed_reason']:<16} \"{n['text']}\"  | "
                             f"{(n['anchor_text'] or '')[:90]}")
    if s["adherence"] is not None:
        lines.append(f"adherence {round(s['adherence'] * 100)}% ({s['outcomes']})")
    else:
        lines.append(f"outcomes {s['outcomes']}")
    return "\n".join(lines)
