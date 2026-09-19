"""Jarvis bridge. sales.db owns sales loops; Jarvis's world.db shows them.

  mirror     confirmed my_action / prospect_action loops land in world.db through
             jarvis's own commitments.py (source_id sales:<loop_id>, so a
             re-land is a no-op). Commitments are never written directly.
  links      every linked loop records who made the link (loops.world_link):
               mirror   we landed it; we may close or drop it
               adopted  the model matched it to an existing Jarvis commitment
               imported bootstrap_import linked it
             Only a mirror is ever dropped in Jarvis. An adopted or imported
             commitment is closed only when the seller confirmed the loop done;
             otherwise a cancelled/rejected loop just loses the link (review
             2026-09-12: rejecting a model's guess must not drop a real
             Jarvis commitment).
  keep-alive commitments.py sweep drops anything idle 14 days; each sync emits
             sales_loop_active on every CONFIRMED linked open commitment.
  read-back  the seller's DONE/DROP in Jarvis flows back as user input. Our own
             closures ("... in sales-coach") are recognised: if the loop is
             open again here, the seller reopened it, so it is reopened in Jarvis
             too. Automatic sweep drops ("auto-dropped: ...") are restored.
  claims     world.db state sales:calls lists calls that reached
             loops_reconciled without error (internal domains removed).
             jarvis-evening skips a Granola meeting only when such a call
             covers it. A failed capture or pipeline claims nothing.
Every step commits before the next subprocess, so the sales.db write lock is
never held while commitments.py runs.
"""
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import seller
from ..memory import gate
from ..store import stores
from ..store.stores import engine, now

PYTHON = "/usr/bin/python3"
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", os.path.expanduser("~/.claude/jarvis")))
DIRECTION = {"my_action": "owed_by_me", "prospect_action": "owed_to_me"}
ACTOR = "sales-coach"
AUTO_DROP_PREFIX = "auto-dropped"
OUR_NOTE = "in sales-coach"
CLAIM_DAYS = 30


class BridgeError(RuntimeError):
    pass


def available() -> bool:
    return (JARVIS_DIR / "commitments.py").exists() and stores.world_available()


def _commitments(*args) -> str:
    env = dict(os.environ)
    env["WORLD_DB"] = str(stores.WORLD_DB)          # the same file this process reads
    proc = subprocess.run([PYTHON, str(JARVIS_DIR / "commitments.py"), *args], cwd=str(JARVIS_DIR),
                          env=env, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise BridgeError((proc.stderr or proc.stdout).strip()[:500])
    return proc.stdout.strip()


def _link(loop) -> str:
    return loop["world_link"] or ("imported" if loop["source"] == "world" else "adopted")


def _unlink(conn, loop_id, why):
    conn.execute("UPDATE loops SET world_commitment_id=NULL, world_link=NULL WHERE node_id=?", (loop_id,))
    engine._emit(conn, ACTOR, "loop_unlinked", node_id=loop_id, after={"why": why})


def _counterparty(conn, loop) -> str:
    """Who the commitment is with: the buyer-side person, else the account."""
    if loop["owner_person_id"]:
        row = conn.execute("SELECT name, email FROM people WHERE node_id=?", (loop["owner_person_id"],)).fetchone()
        if row:
            return row["email"] or row["name"]
    if loop["call_id"]:
        row = conn.execute(
            "SELECT p.email, p.name FROM call_participants cp JOIN people p ON p.node_id=cp.person_id "
            "WHERE cp.call_id=? AND p.is_me=0 ORDER BY p.email IS NULL, p.name LIMIT 1",
            (loop["call_id"],)).fetchone()
        if row:
            return row["email"] or row["name"]
    if loop["owner_name"]:
        return loop["owner_name"]
    row = conn.execute("SELECT a.name FROM deals d JOIN accounts a ON a.node_id=d.account_id WHERE d.node_id=?",
                       (loop["deal_id"],)).fetchone()
    return row["name"] if row else "unknown"


def mirror_loop(conn, loop):
    if (loop["type"] not in DIRECTION or loop["review_state"] != "confirmed"
            or loop["status"] not in ("open", "waiting") or loop["world_commitment_id"]):
        return None
    payload = {"text": loop["description"], "quote": loop["evidence_quote"] or loop["description"],
               "direction": DIRECTION[loop["type"]], "counterparty": _counterparty(conn, loop),
               "due": loop["due_date"], "channel": "sales-coach", "source_id": f"sales:{loop['node_id']}"}
    result = json.loads(_commitments("land", json.dumps(payload)).splitlines()[-1])
    conn.execute("UPDATE loops SET world_commitment_id=?, world_link='mirror' WHERE node_id=?",
                 (result["id"], loop["node_id"]))
    engine._emit(conn, ACTOR, "loop_mirrored", node_id=loop["node_id"],
                 after={"world_id": result["id"], "result": result["result"]})
    conn.commit()
    return result["id"]


def _world_state(wconn, world_id):
    node = wconn.execute("SELECT status FROM nodes WHERE id=?", (world_id,)).fetchone()
    if node is None:
        return None, None
    ev = wconn.execute("SELECT after FROM events WHERE node_id=? AND kind IN ('commitment_closed','commitment_dropped') "
                       "ORDER BY id DESC LIMIT 1", (world_id,)).fetchone()
    note = (json.loads(ev["after"] or "{}").get("note") or "") if ev else ""
    return node["status"], note


def _states(ids):
    wconn = stores.world_ro()
    try:
        return {i: _world_state(wconn, i) for i in ids}
    finally:
        wconn.close()


def _status_set_by_user(conn, loop_id) -> bool:
    row = conn.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='status'",
                       (loop_id,)).fetchone()
    return bool(row) and row["confidence"] == "user_input"


def close_mirrors(conn) -> list:
    """Loops closed here whose linked Jarvis commitment is still open."""
    done = []
    rows = conn.execute("SELECT * FROM loops WHERE world_commitment_id IS NOT NULL "
                        "AND status IN ('done','cancelled','superseded')").fetchall()
    if not rows:
        return done
    states = _states({r["world_commitment_id"] for r in rows})
    for loop in rows:
        world_id = loop["world_commitment_id"]
        status, _ = states[world_id]
        link = _link(loop)
        if status != "full":
            continue
        if link == "mirror":
            if loop["status"] == "done":
                _commitments("close", world_id, "--evidence", f"closed {OUR_NOTE} ({loop['node_id']})")
            else:
                _commitments("drop", world_id, "--reason", f"{loop['status']} {OUR_NOTE} ({loop['node_id']})")
            done.append(loop["node_id"])
        elif loop["review_state"] == "confirmed" and loop["status"] == "done":
            if not _status_set_by_user(conn, loop["node_id"]):
                continue        # a reply or a model said done; Jarvis waits for the seller's confirmation
            _commitments("close", world_id, "--evidence", f"done {OUR_NOTE} ({loop['node_id']})")
            done.append(loop["node_id"])
        else:
            _unlink(conn, loop["node_id"], f"{link} link released: loop {loop['status']} here, Jarvis keeps its own")
        conn.commit()
    return done


def read_back(conn) -> list:
    """The seller's DONE/DROP in Jarvis flows back; our own closures and sweep drops are reopened."""
    changes = []
    rows = conn.execute("SELECT * FROM loops WHERE world_commitment_id IS NOT NULL AND review_state='confirmed' "
                        "AND status IN ('open','waiting')").fetchall()
    if not rows:
        return changes
    states = _states({r["world_commitment_id"] for r in rows})
    for loop in rows:
        world_id = loop["world_commitment_id"]
        status, note = states[world_id]
        if status in (None, "full", "candidate"):
            continue
        if OUR_NOTE in note:
            # We closed it because the loop was done here; it is open again, so the seller reopened it.
            _commitments("restore", world_id, "--reason", f"reopened {OUR_NOTE}")
            changes.append({"loop": loop["node_id"], "action": "reopened_in_jarvis"})
        elif status == "deprecated" and note.startswith(AUTO_DROP_PREFIX):
            _commitments("restore", world_id, "--reason", f"still open {OUR_NOTE}")
            changes.append({"loop": loop["node_id"], "action": "restored_in_jarvis"})
        else:
            new_status = "done" if status == "closed" else "cancelled"
            gate.propose(conn, gate.Proposed(loop["node_id"], "loops", "status", new_status, "user_input",
                                             {"kind": "user_input", "ref": f"jarvis:{world_id}", "note": note}),
                         actor=ACTOR)
            conn.execute("UPDATE loops SET closed_at=?, last_activity_at=? WHERE node_id=?",
                         (now(), now(), loop["node_id"]))
            changes.append({"loop": loop["node_id"], "action": new_status, "note": note})
        conn.commit()
    return changes


def keep_alive(conn) -> int:
    rows = conn.execute("SELECT node_id, world_commitment_id FROM loops WHERE world_commitment_id IS NOT NULL "
                        "AND review_state='confirmed' AND status IN ('open','waiting')").fetchall()
    if not rows:
        return 0
    wconn = stores.world_rw()
    count = 0
    try:
        for loop in rows:
            node = wconn.execute("SELECT status FROM nodes WHERE id=?", (loop["world_commitment_id"],)).fetchone()
            if node and node["status"] == "full":
                engine._emit(wconn, ACTOR, "sales_loop_active", node_id=loop["world_commitment_id"],
                             after={"loop_id": loop["node_id"]})
                count += 1
        wconn.commit()
    finally:
        wconn.close()
    return count


def claimable_states() -> list:
    from ..orchestrator import workflow
    names = workflow.STEP_NAMES
    return names[names.index("loops_reconciled"):] + list(workflow.POST_PIPELINE)


def claim_calls(conn) -> int:
    """Publish the calls this system really processed, for jarvis-evening's skip rule."""
    since = (datetime.now(timezone.utc) - timedelta(days=CLAIM_DAYS)).isoformat(timespec="seconds")
    states = claimable_states()
    marks = ",".join("?" * len(states))
    calls = []
    for c in conn.execute(f"SELECT node_id, started_at, ended_at FROM calls WHERE wf_state IN ({marks}) "
                          "AND wf_error IS NULL AND history=0 AND started_at>=? ORDER BY started_at",
                          (*states, since)):
        emails = [r["email"] for r in conn.execute(
            "SELECT p.email FROM call_participants cp JOIN people p ON p.node_id=cp.person_id "
            "WHERE cp.call_id=? AND p.is_me=0 AND p.email IS NOT NULL", (c["node_id"],))]
        domains = sorted({e.split("@", 1)[1] for e in emails} - seller.internal_domains())
        if domains:
            calls.append({"call_id": c["node_id"], "started_at": c["started_at"], "ended_at": c["ended_at"],
                          "domains": domains})
    wconn = stores.world_rw()
    try:
        wconn.execute("INSERT INTO state(key,value,updated_at) VALUES ('sales:calls',?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (json.dumps(calls), now()))
        wconn.commit()
    finally:
        wconn.close()
    return len(calls)


def sync(conn, call_id=None) -> dict:
    """Run every bridge duty. Safe to call as often as wanted."""
    if not available():
        return {"skipped": "jarvis world.db or commitments.py not found"}
    conn.commit()
    mirrored = []
    for loop in conn.execute("SELECT * FROM loops WHERE review_state='confirmed' AND world_commitment_id IS NULL "
                             "AND status IN ('open','waiting') AND type IN ('my_action','prospect_action')").fetchall():
        world_id = mirror_loop(conn, loop)
        if world_id:
            mirrored.append(world_id)
    result = {"mirrored": mirrored, "closed_in_jarvis": close_mirrors(conn), "read_back": read_back(conn)}
    result["kept_alive"] = keep_alive(conn)
    result["claimed_calls"] = claim_calls(conn)
    return result


def _matches_person(counterparty: str, emails: set, names: set, domains: set) -> bool:
    cp = (counterparty or "").lower().strip()
    if not cp:
        return False
    if cp in emails:
        return True
    if "@" in cp and cp.split("@", 1)[1] in domains:
        return True
    # Whole-name matches only ("raj" must not match "rajesh@...").
    return any(len(n.split()) >= 2 and re.search(rf"\b{re.escape(n)}\b", cp) for n in names)


def bootstrap_import(conn, deal_id, dry_run=False) -> dict:
    """One-time: bring Jarvis's existing open commitments for this deal's people
    into sales.db as confirmed loops, linked by id (no new world rows)."""
    deal = conn.execute("SELECT d.*, a.domains FROM deals d LEFT JOIN accounts a ON a.node_id=d.account_id "
                        "WHERE d.node_id=?", (deal_id,)).fetchone()
    if deal is None:
        raise KeyError(deal_id)
    domains = set(json.loads(deal["domains"] or "[]"))
    people = conn.execute("SELECT p.name, p.email FROM deal_people dp JOIN people p ON p.node_id=dp.person_id "
                          "WHERE dp.deal_id=? AND p.is_me=0", (deal_id,)).fetchall()
    emails = {p["email"].lower() for p in people if p["email"]}
    names = {p["name"].lower() for p in people if p["name"]}
    linked = {r["world_commitment_id"] for r in conn.execute(
        "SELECT world_commitment_id FROM loops WHERE world_commitment_id IS NOT NULL")}
    wconn = stores.world_ro()
    try:
        rows = wconn.execute(
            "SELECT n.id, n.title, c.direction, c.due, c.counterparty, c.quote, s.uri FROM nodes n "
            "JOIN commitments c ON c.node_id=n.id LEFT JOIN sources s ON s.node_id=n.id "
            "WHERE n.status='full' AND c.closed_at IS NULL").fetchall()
    finally:
        wconn.close()
    matches = [dict(r) for r in rows if r["id"] not in linked and not (r["uri"] or "").startswith("sales:")
               and _matches_person(r["counterparty"], emails, names, domains)]
    if dry_run:
        return {"would_import": matches}
    created = []
    for m in matches:
        loop_id = f"loop-w-{m['id'][-12:]}"
        loop_type = "my_action" if m["direction"] == "owed_by_me" else "prospect_action"
        engine.add_node(conn, ACTOR, id=loop_id, type="loop", kind=loop_type, title=m["title"][:200],
                        status="full", confidence=2 / 3, source_id=m["id"])
        conn.execute(
            "INSERT INTO loops(node_id,deal_id,type,description,owner,source,confidence,evidence_quote,due_date,"
            "due_date_confidence,status,review_state,world_commitment_id,world_link,created_at,last_activity_at) "
            "VALUES (?,?,?,?,?,'world','high',?,?,?,'open','confirmed',?,'imported',?,?)",
            (loop_id, deal_id, loop_type, m["title"], "me" if loop_type == "my_action" else "prospect",
             m["quote"], m["due"], "explicit" if m["due"] else "unknown", m["id"], now(), now()))
        engine.set_source(conn, ACTOR, loop_id, uri=f"world:{m['id']}", capture="world")
        # Imported at the seller's request, so the status is theirs (user_input): a reply can propose "done"
        # but cannot apply it, and the Jarvis commitment closes only when he confirms.
        gate.set_initial(conn, loop_id, "loops", "status", "open", "user_input", {"kind": "user_input", "ref": m["id"]})
        created.append(loop_id)
    conn.commit()
    return {"imported": created}
