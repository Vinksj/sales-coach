"""Builders shared by the learning-layer tests (no tests in this module). Offline, no model."""
import json
from datetime import datetime, timedelta, timezone

from salescoach import repo
from salescoach.orchestrator import context
from salescoach.store.stores import now
from salescoach.store.db import insert_id

BASE = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def day(n: int) -> str:
    """ISO timestamp n days after the base date; calls are created oldest first with rising n."""
    return (BASE + timedelta(days=n)).isoformat(timespec="seconds")


def make_deal(db, name="Deal", domain=None, stage="discovery") -> str:
    acct = repo.create_account(db, f"{name} Inc", [domain or f"{name.lower().replace(' ', '')}.test"])
    return repo.create_deal(db, name, account_id=acct, stage=stage)


def make_call(db, deal_id, n: int, analysed=True, source="paste", title=None, layout=None) -> str:
    call_id = repo.create_call(db, source=source, title=title or f"call {n}", deal_id=deal_id, started_at=day(n),
                               wf_state="complete")
    if analysed:
        context.save_artifact(db, call_id, "analysis", {"verdict": {"label": "ok", "one_line": "x"}})
    if layout:
        db.execute("UPDATE sources SET lineage=? WHERE node_id=?", (json.dumps([{"file": "a.wav", "layout": layout}]), call_id))
    return call_id


def tag(db, call_id, name, polarity="weakness", confidence="high", severity="medium", turns=(3,)):
    db.execute("INSERT INTO seller_observations(call_id,tag,polarity,severity,contexts,evidence_turns,evidence_quote,"
               "confidence,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (call_id, name, polarity, severity, "[]", json.dumps(list(turns)), "a quote from the call", confidence, now()))


def calls_with_tag(db, name, plan, **kw) -> list[str]:
    """plan: [(deal_id, n_day, has_tag)], created in the order given. Returns the call ids."""
    ids = []
    for deal_id, n, has in plan:
        cid = make_call(db, deal_id, n)
        if has:
            tag(db, cid, name, **kw)
        ids.append(cid)
    db.commit()
    return ids


def final_snapshot(db, call_id, session="live-1", me_share=0.4, questions=7, known=2, partial=1, objections=()):
    slots = {f"s{i}": {"status": "known" if i < known else ("partial" if i < known + partial else "unknown")}
             for i in range(10)}
    snap = {"final": True, "me_share": me_share, "me_questions": questions, "slots": slots,
            "open_objections": [{"t": 10.0 * i, "category": c, "text": "the buyer's words"} for i, c in enumerate(objections)]}
    db.execute("INSERT INTO coach_state(call_id,session,t_call,json,created_at) VALUES (?,?,?,?,?)",
               (call_id, session, 1800.0, json.dumps(snap), now()))


def live_nudge(db, call_id, trigger, outcome="ignored", dismissed=0, mode="live", shown=1, session=None):
    cur = db.execute(
        "INSERT INTO nudges(call_id,session,mode,t_call,trigger,text,kind,source,shown,dismissed,outcome,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (call_id, session or f"{mode}-1", mode, 120.0, trigger, "Ask who else is involved", "moment", "fast", shown,
         dismissed, outcome, now()))
    return insert_id(cur)


def sent_edit(db, deal_id, draft, final, sent_at=None, kind="followup", call_id=None) -> int:
    cur = db.execute(
        "INSERT INTO emails(call_id,deal_id,kind,to_addrs,subject,body,draft_body,status,sent_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,'sent',?,?)",
        (call_id, deal_id, kind, json.dumps(["buyer@x.test"]), "Following up", final, draft, sent_at or day(1), day(0)))
    db.execute("INSERT INTO email_edits(email_id,draft_body,final_body,created_at) VALUES (?,?,?,?)",
               (insert_id(cur), draft, final, sent_at or day(1)))
    return insert_id(cur)


def pattern(db, pid):
    row = db.execute("SELECT * FROM learned_patterns WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None
