"""The seller's review of a processed call: the human gate between analysis and action.

Confirming a loop is user input: it becomes the loop's strongest provenance
and is the only way an inferred item becomes something the system acts on
with full confidence. After review, confirmed loops are mirrored to Jarvis.
"""
import json

from .. import repo
from ..memory import gate
from ..schemas.events import Event
from ..store.stores import engine, now
from . import bus

ACTOR = "user"
EDITABLE = {"description", "owner", "owner_name", "priority", "due_date", "status"}


def confirm_loop(conn, loop_id):
    conn.execute("UPDATE loops SET review_state='confirmed', last_activity_at=? WHERE node_id=?", (now(), loop_id))
    gate.set_initial(conn, loop_id, "loops", "status",
                     conn.execute("SELECT status FROM loops WHERE node_id=?", (loop_id,)).fetchone()["status"],
                     "user_input", {"kind": "user_input", "ref": "review"})
    engine._emit(conn, ACTOR, "loop_confirmed", node_id=loop_id)


def reject_loop(conn, loop_id, reason="rejected in review"):
    # Rejecting a model-guessed Jarvis link only releases the link; the Jarvis commitment is untouched.
    conn.execute("UPDATE loops SET review_state='rejected', status='cancelled', closed_at=?, last_activity_at=?, "
                 "world_commitment_id=CASE WHEN world_link='mirror' THEN world_commitment_id END, "
                 "world_link=CASE WHEN world_link='mirror' THEN world_link END WHERE node_id=?",
                 (now(), now(), loop_id))
    engine._emit(conn, ACTOR, "loop_rejected", node_id=loop_id, after={"reason": reason})


def edit_loop(conn, loop_id, **fields):
    for field, value in fields.items():
        if field not in EDITABLE:
            raise ValueError(field)
        if field == "due_date" and value:
            conn.execute("UPDATE loops SET due_date_confidence='explicit' WHERE node_id=?", (loop_id,))
        gate.propose(conn, gate.Proposed(loop_id, "loops", field, value or None, "user_input",
                                         {"kind": "user_input", "ref": "review"}), actor=ACTOR)
    if fields.get("status") in ("done", "cancelled", "superseded"):
        conn.execute("UPDATE loops SET closed_at=? WHERE node_id=?", (now(), loop_id))
    conn.execute("UPDATE loops SET review_state='confirmed', last_activity_at=? WHERE node_id=?", (now(), loop_id))


def add_loop(conn, call_id, description, owner, type_, due_date=None, priority="medium"):
    """A loop the seller adds by hand: user_input provenance, confirmed on creation."""
    call = repo.get_call(conn, call_id)
    loop_id = repo.new_id("loop")
    engine.add_node(conn, ACTOR, id=loop_id, type="loop", kind=type_, title=description[:200], status="full",
                    confidence=1.0, source_id=call_id)
    conn.execute(
        "INSERT INTO loops(node_id,deal_id,call_id,type,description,owner,source,confidence,priority,due_date,"
        "due_date_confidence,status,review_state,created_at,last_activity_at) "
        "VALUES (?,?,?,?,?,?,'user_input','explicit',?,?,?,'open','confirmed',?,?)",
        (loop_id, call["deal_id"], call_id, type_, description, owner, priority, due_date,
         "explicit" if due_date else "unknown", now(), now()))
    gate.set_initial(conn, loop_id, "loops", "status", "open", "user_input", {"kind": "user_input"})
    return loop_id


def resolve_proposal(conn, conflict_id, accept: bool):
    ok = gate.resolve_conflict(conn, conflict_id, accept, actor=ACTOR)
    if ok and accept:
        row = conn.execute("SELECT entity_id, proposed_value, field FROM memory_conflicts WHERE id=?",
                           (conflict_id,)).fetchone()
        if row["field"] == "loops.status" and row["proposed_value"] in ("done", "cancelled", "superseded"):
            conn.execute("UPDATE loops SET closed_at=? WHERE node_id=?", (now(), row["entity_id"]))
    return ok


def complete_review(conn, call_id):
    """Close the review: anything still proposed stays proposed (visible in Open Loops).

    Only an awaiting_review call moves to 'reviewed'; a call whose email was
    already sent or skipped keeps its later state. Mirroring to Jarvis runs
    either way.
    """
    if repo.get_call(conn, call_id)["wf_state"] == "awaiting_review":
        repo.set_call_state(conn, call_id, "reviewed", actor=ACTOR)
    bus.publish(conn, Event(type="REVIEW_COMPLETED", entity_id=call_id, dedupe_key=f"REVIEW:{call_id}:{now()}"))
    conn.commit()


def after_review(conn, call_id):
    """Worker handler for REVIEW_COMPLETED: mirror confirmed loops to Jarvis, record the deal's next step."""
    from ..integrations import jarvis_bridge
    jarvis_bridge.sync(conn, call_id=call_id)
    conn.commit()


def email_done(conn, call_id, sent: bool):
    statuses = {r["status"] for r in conn.execute("SELECT status FROM emails WHERE call_id=?", (call_id,))}
    if "sending" in statuses:
        raise ValueError("an email for this call may already have gone out; check Gmail Sent and resolve it first")
    sent = sent or "sent" in statuses
    if not sent:
        # Closing without an email retires every unsent draft, so nothing sendable is left on a closed call.
        conn.execute("UPDATE emails SET status='rejected', error='closed without email', updated_at=? "
                     "WHERE call_id=? AND status IN ('drafted','failed')", (now(), call_id))
    repo.set_call_state(conn, call_id, "email_sent" if sent else "email_skipped", actor=ACTOR)
    repo.set_call_state(conn, call_id, "done", actor=ACTOR)
    conn.commit()


def skip_email(conn, email_id, reason="skipped in review"):
    row = conn.execute("SELECT call_id, status FROM emails WHERE id=?", (email_id,)).fetchone()
    if row and row["status"] in ("drafted", "failed"):
        conn.execute("UPDATE emails SET status='rejected', error=?, updated_at=? WHERE id=?",
                     (reason, now(), email_id))
    return row["call_id"] if row else None


def update_email(conn, email_id, subject, body, to, cc):
    row = conn.execute("SELECT status FROM emails WHERE id=?", (email_id,)).fetchone()
    if row is None or row["status"] not in ("drafted", "failed"):
        raise ValueError("only a draft can be edited")
    conn.execute("UPDATE emails SET subject=?, body=?, to_addrs=?, cc_addrs=?, status='drafted', updated_at=? "
                 "WHERE id=?", (subject, body, json.dumps(to), json.dumps(cc), now(), email_id))
