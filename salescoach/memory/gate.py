"""The memory gate: how a proposed fact becomes stored memory (spec §15).

  candidate -> confidence -> conflict detection -> compare with existing
            -> proposed update -> validation -> persist with provenance

Rules:
  * the seller's own input (provenance kind user_input) always wins and closes any
    open conflict on that field.
  * A value backed by explicit evidence or user input is never overwritten by
    a weaker claim; the disagreement is recorded in memory_conflicts instead.
  * Otherwise an equal-or-stronger claim replaces the value.
  * Every applied change emits an event with before/after and records the new
    value's confidence and provenance in field_provenance.
"""
import json
from dataclasses import dataclass, field

from ..store.stores import engine, now

RANK = {"low": 0, "medium": 1, "high": 2, "explicit": 3, "user_input": 4}

# Tables and columns the gate may write. Keeps a model's field name out of SQL.
GATED = {
    "deals": ("node_id", {"stage", "status", "next_step", "close_target"}),
    "people": ("node_id", {"name", "email", "title", "account_id"}),
    "deal_people": None,  # handled by repo.link_deal_person
    "loops": ("node_id", {"status", "due_date", "due_date_confidence", "priority", "description",
                          "owner", "owner_name", "superseded_by", "follow_up_strategy"}),
}


def register_table(table: str, key_col: str, fields) -> None:
    """Plugins put their own tables behind the gate. The key must be ONE column
    (use a surrogate like '<deal_id>:<person_id>' for composite keys)."""
    GATED[table] = (key_col, set(fields))


@dataclass
class Proposed:
    entity_id: str
    table: str
    field: str
    value: object
    confidence: str                   # low|medium|high|explicit|user_input
    provenance: dict = field(default_factory=dict)


def _current(conn, p: Proposed):
    key, columns = GATED[p.table]
    if p.field not in columns:
        raise ValueError(f"{p.table}.{p.field} is not a gated field")
    row = conn.execute(f"SELECT {p.field} AS v FROM {p.table} WHERE {key}=?", (p.entity_id,)).fetchone()
    if row is None:
        raise KeyError(f"{p.table} {p.entity_id} not found")
    prov = conn.execute("SELECT confidence, provenance FROM field_provenance WHERE entity_id=? AND field=?",
                        (p.entity_id, p.field)).fetchone()
    return row["v"], (prov["confidence"] if prov else None), (prov["provenance"] if prov else None)


def propose(conn, p: Proposed, actor="memory_gate") -> str:
    """Apply or park a proposed value. Returns applied | unchanged | conflict."""
    existing, existing_conf, _ = _current(conn, p)
    value = p.value if not isinstance(p.value, (list, dict)) else json.dumps(p.value)
    if existing == value:
        if existing_conf is None or RANK[p.confidence] > RANK[existing_conf]:
            _record_provenance(conn, p, value)
        return "unchanged"
    is_user = p.provenance.get("kind") == "user_input" or p.confidence == "user_input"
    # existing_conf alone decides: a value the seller CLEARED (None, user_input) is as protected as one they set.
    if not is_user and existing_conf is not None and RANK[p.confidence] < RANK[existing_conf]:
        _conflict(conn, p, existing, existing_conf, value)
        return "conflict"
    _apply(conn, p, existing, value, actor)
    if is_user:
        # The seller decided: the proposal they chose is accepted, every competing one rejected.
        chosen = None if value is None else str(value)
        conn.execute("UPDATE memory_conflicts SET status=CASE WHEN proposed_value IS ? THEN 'accepted' "
                     "ELSE 'rejected' END, resolved_at=? WHERE entity_id=? AND field=? AND status='open'",
                     (chosen, now(), p.entity_id, _qualified(p)))
    return "applied"


def _qualified(p: Proposed) -> str:
    return f"{p.table}.{p.field}"


def _apply(conn, p, existing, value, actor):
    key, _ = GATED[p.table]
    conn.execute(f"UPDATE {p.table} SET {p.field}=? WHERE {key}=?", (value, p.entity_id))
    _record_provenance(conn, p, value)
    engine._emit(conn, actor, f"{p.table}_{p.field}_changed", node_id=p.entity_id,
                 before={p.field: existing}, after={p.field: value, "confidence": p.confidence},
                 source_id=p.provenance.get("ref"))


def _record_provenance(conn, p, value):
    conn.execute(
        "INSERT INTO field_provenance(entity_id,field,value,confidence,provenance,updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(entity_id,field) DO UPDATE SET value=excluded.value, confidence=excluded.confidence, "
        "provenance=excluded.provenance, updated_at=excluded.updated_at",
        (p.entity_id, p.field, None if value is None else str(value), p.confidence,
         json.dumps(p.provenance), now()))


def _conflict(conn, p, existing, existing_conf, value):
    dup = conn.execute(
        "SELECT 1 FROM memory_conflicts WHERE entity_id=? AND field=? AND proposed_value IS ? AND status='open'",
        (p.entity_id, _qualified(p), None if value is None else str(value))).fetchone()
    if dup:
        return
    conn.execute(
        "INSERT INTO memory_conflicts(entity_id,field,existing_value,existing_confidence,proposed_value,"
        "proposed_confidence,provenance,status,created_at) VALUES (?,?,?,?,?,?,?,'open',?)",
        (p.entity_id, _qualified(p), None if existing is None else str(existing), existing_conf,
         None if value is None else str(value), p.confidence, json.dumps(p.provenance), now()))
    engine._emit(conn, "memory_gate", "memory_conflict", node_id=p.entity_id,
                 before={p.field: existing}, after={p.field: value, "confidence": p.confidence})


def park(conn, p: Proposed):
    """Record a proposal too weak to apply on its own; the seller accepts or rejects it in review."""
    existing, existing_conf, _ = _current(conn, p)
    value = p.value if not isinstance(p.value, (list, dict)) else json.dumps(p.value)
    if existing == value:
        return "unchanged"
    _conflict(conn, p, existing, existing_conf, value)
    return "needs_review"


def set_initial(conn, entity_id, table, fld, value, confidence, provenance):
    """Record provenance for a value written at creation time."""
    _record_provenance(conn, Proposed(entity_id, table, fld, value, confidence, provenance),
                       value if not isinstance(value, (list, dict)) else json.dumps(value))


def resolve_conflict(conn, conflict_id: int, accept: bool, actor="user"):
    """The seller settles a conflict from the UI. Accepting applies the proposed value as user input."""
    row = conn.execute("SELECT * FROM memory_conflicts WHERE id=?", (conflict_id,)).fetchone()
    if row is None or row["status"] != "open":
        return False
    if accept:
        table, fld = row["field"].split(".", 1)
        propose(conn, Proposed(row["entity_id"], table, fld, row["proposed_value"], "user_input",
                               {"kind": "user_input", "ref": f"conflict:{conflict_id}"}), actor=actor)
    conn.execute("UPDATE memory_conflicts SET status=?, resolved_at=? WHERE id=?",
                 ("accepted" if accept else "rejected", now(), conflict_id))
    return True
