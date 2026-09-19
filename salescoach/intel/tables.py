"""The intelligence tables behind the memory gate, and the reads every view shares.

Importing this module registers the four tables with the gate, so the core's
conflict-resolution route can settle a stakeholder or methodology-element
disagreement the same way it settles a loop status. The `meddpicc` table holds
the elements of WHATEVER methodology is active (the name is historical); reads
return the active methodology's elements only, so rows written under another
one stay stored and out of sight. Writes follow the gate's rules:
a strategist claim never overwrites the seller's edit (user_input) or a stronger
claim; it becomes a memory_conflicts row instead.
"""
import json

from ..memory import gate
from ..store.stores import engine
from . import methodology

GATED_FIELDS = {
    "stakeholders": ("role", "influence", "incentives", "concerns", "relationship_strength", "position",
                     "champion_potential", "ability_to_block"),
    "meddpicc": ("status", "what_we_know", "gap", "next_question"),
    "deal_risks": ("severity", "status", "description", "mitigation"),
    "deal_health": ("score", "label", "rationale"),
}
for _table, _fields in GATED_FIELDS.items():
    gate.register_table(_table, "id", _fields)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
POSITION_ORDER = {"champion": 0, "supporter": 1, "neutral": 2, "skeptic": 3, "blocker": 4, "unknown": 5}
ACTOR = "deal_strategist"


def stakeholder_id(deal_id, person_id):
    return f"{deal_id}:{person_id}"


def meddpicc_id(deal_id, element):
    return f"{deal_id}:{element}"


def risk_id(deal_id, risk_type):
    # "risk:" keeps this apart from meddpicc_id: both tables gate a field called status, and field
    # provenance is keyed on (entity_id, field), so "<deal>:competition" used to be shared by the
    # MEDDPICC competition element and the competition risk.
    return f"{deal_id}:risk:{risk_type}"


def migrate_risk_ids(conn) -> int:
    """Rename risk rows created with the old colliding id form. Idempotent; returns rows moved."""
    moved = 0
    for r in conn.execute("SELECT id, deal_id, type FROM deal_risks WHERE id NOT LIKE '%:risk:%'").fetchall():
        new_id = risk_id(r["deal_id"], r["type"])
        if conn.execute("SELECT 1 FROM deal_risks WHERE id=?", (new_id,)).fetchone():
            conn.execute("DELETE FROM deal_risks WHERE id=?", (r["id"],))
        else:
            conn.execute("UPDATE deal_risks SET id=? WHERE id=?", (new_id, r["id"]))
        # The old provenance row may have been shared with the MEDDPICC element of the same name, so it
        # is copied for the risk (keeping whatever protection it carried) and left in place for MEDDPICC.
        for p in conn.execute("SELECT * FROM field_provenance WHERE entity_id=? AND field IN ('severity','status',"
                              "'description','mitigation')", (r["id"],)).fetchall():
            cols = [k for k in p.keys() if k != "id"]
            vals = [new_id if k == "entity_id" else p[k] for k in cols]
            conn.execute(f"INSERT OR IGNORE INTO field_provenance({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         vals)
        conn.execute("UPDATE memory_conflicts SET entity_id=? WHERE entity_id=? AND field LIKE 'deal_risks.%'",
                     (new_id, r["id"]))
        moved += 1
    if moved:
        conn.commit()       # readers (page loads) never commit; the rename must not roll back with the request
    return moved


def health_id(deal_id):
    return f"{deal_id}:health"


def _sql_value(v):
    return json.dumps(v) if isinstance(v, (list, dict)) else v


def upsert(conn, table, row_id, base: dict, fields: dict, confidence: str, provenance: dict,
           direct: dict | None = None, actor=ACTOR) -> dict:
    """Create the row, or propose each gated field through the gate.

    fields: gated values (None means "no opinion" and is skipped).
    direct: bookkeeping columns written as-is (evidence of the latest read, run id).
    Returns {field: applied|unchanged|conflict}.
    """
    direct = dict(direct or {})
    fields = {k: v for k, v in fields.items() if v is not None}
    if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (row_id,)).fetchone() is None:
        cols = {"id": row_id, **base, **{k: _sql_value(v) for k, v in fields.items()},
                **{k: _sql_value(v) for k, v in direct.items()}}
        conn.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     tuple(cols.values()))
        for k, v in fields.items():
            gate.set_initial(conn, row_id, table, k, v, confidence, provenance)
        engine._emit(conn, actor, f"{table}_created", node_id=row_id,
                     after={k: _sql_value(v) for k, v in fields.items()}, source_id=provenance.get("ref"))
        return {k: "applied" for k in fields}
    outcomes = {k: gate.propose(conn, gate.Proposed(row_id, table, k, v, confidence, provenance), actor=actor)
                for k, v in fields.items()}
    if direct:
        sets = ", ".join(f"{k}=?" for k in direct)
        conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*(_sql_value(v) for v in direct.values()), row_id))
    return outcomes


def provenance_map(conn, deal_id) -> dict:
    """{entity_id: {field: {"confidence", "kind", "ref"}}} for every gated intel field of a deal."""
    out = {}
    for r in conn.execute("SELECT entity_id, field, confidence, provenance FROM field_provenance "
                          "WHERE entity_id LIKE ?", (f"{deal_id}:%",)):
        prov = _loads(r["provenance"], {})
        out.setdefault(r["entity_id"], {})[r["field"]] = {
            "confidence": r["confidence"], "kind": prov.get("kind"), "ref": prov.get("ref")}
    return out


def user_set(prov: dict) -> set:
    return {f for f, p in (prov or {}).items() if p["confidence"] == "user_input" or p["kind"] == "user_input"}


def _loads(raw, default):
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ---- reads -------------------------------------------------------------------

def stakeholders(conn, deal_id) -> list[dict]:
    """Everyone on the deal (except the seller), joined with their map row and field provenance."""
    prov = provenance_map(conn, deal_id)
    rows = conn.execute(
        "SELECT p.node_id AS person_id, p.name, p.email, p.title, p.contact_file, dp.role_in_deal, "
        "s.role, s.influence, s.incentives, s.concerns, s.relationship_strength, s.position, "
        "s.champion_potential, s.ability_to_block, s.evidence, s.confidence, s.run_id, s.updated_at "
        "FROM deal_people dp JOIN people p ON p.node_id=dp.person_id "
        "LEFT JOIN stakeholders s ON s.id = dp.deal_id || ':' || dp.person_id "
        "WHERE dp.deal_id=? AND p.is_me=0", (deal_id,)).fetchall()
    out = []
    for r in rows:
        s = dict(r)
        s["person_id"] = r["person_id"]
        s["id"] = stakeholder_id(deal_id, r["person_id"])
        s["incentives"] = _loads(s.get("incentives"), []) or []
        s["concerns"] = _loads(s.get("concerns"), []) or []
        s["evidence"] = _loads(s.get("evidence"), []) or []
        s["prov"] = prov.get(s["id"], {})
        s["user_set"] = user_set(s["prov"])
        s["mapped"] = r["role"] is not None or r["position"] is not None
        out.append(s)
    out.sort(key=lambda s: (POSITION_ORDER.get(s.get("position") or "unknown", 5),
                            {"high": 0, "medium": 1, "low": 2}.get(s.get("influence") or "", 3), s["name"]))
    return out


def meddpicc_rows(conn, deal_id, m=None) -> list[dict]:
    """One row per element of the active methodology, in its order; an element never assessed is a blank
    row. Rows stored for elements of another methodology are not returned."""
    m = m or methodology.active()
    prov = provenance_map(conn, deal_id)
    have = {r["element"]: dict(r) for r in conn.execute("SELECT * FROM meddpicc WHERE deal_id=?", (deal_id,))}
    out = []
    for e in m.elements:
        el = e.key
        row = have.get(el) or {"id": meddpicc_id(deal_id, el), "element": el, "status": None, "what_we_know": None,
                               "gap": None, "next_question": None, "evidence": "[]", "confidence": None}
        row["label"] = e.label
        row["critical"] = e.critical
        row["evidence"] = _loads(row.get("evidence"), []) or []
        row["prov"] = prov.get(row["id"], {})
        row["user_set"] = user_set(row["prov"])
        out.append(row)
    return out


def risks(conn, deal_id, include_closed=False) -> list[dict]:
    migrate_risk_ids(conn)
    prov = provenance_map(conn, deal_id)
    rows = [dict(r) for r in conn.execute("SELECT * FROM deal_risks WHERE deal_id=?", (deal_id,))]
    out = []
    for r in rows:
        if not include_closed and r["status"] != "open":
            continue
        r["evidence"] = _loads(r.get("evidence"), []) or []
        r["prov"] = prov.get(r["id"], {})
        r["user_set"] = user_set(r["prov"])
        out.append(r)
    out.sort(key=lambda r: (r["status"] != "open", SEVERITY_ORDER.get(r["severity"] or "", 4), r["type"]))
    return out


def health(conn, deal_id) -> dict | None:
    row = conn.execute("SELECT * FROM deal_health WHERE deal_id=?", (deal_id,)).fetchone()
    if row is None:
        return None
    h = dict(row)
    h["caps"] = _loads(h.get("caps"), []) or []
    h["next_best_action"] = _loads(h.get("next_best_action"), None)
    h["prov"] = provenance_map(conn, deal_id).get(h["id"], {})
    h["history"] = [dict(r) for r in conn.execute(
        "SELECT score, label, call_id, created_at FROM deal_health_history WHERE deal_id=? ORDER BY id DESC LIMIT 8",
        (deal_id,))][::-1]
    return h


def conflicts(conn, deal_id) -> list[dict]:
    """Open gate conflicts on this deal's intelligence rows. A conflict on an element the active
    methodology does not have is left open and not shown: it comes back with its methodology."""
    active_keys = set(methodology.active().keys)
    out = []
    for r in conn.execute("SELECT * FROM memory_conflicts WHERE status='open' AND entity_id LIKE ? AND "
                          "(field LIKE 'stakeholders.%' OR field LIKE 'meddpicc.%' OR field LIKE 'deal_risks.%' "
                          "OR field LIKE 'deal_health.%') ORDER BY id", (f"{deal_id}:%",)):
        c = dict(r)
        table, fld = c["field"].split(".", 1)
        key = c["entity_id"].split(":", 1)[1]
        if table == "stakeholders":
            person = conn.execute("SELECT name FROM people WHERE node_id=?", (key,)).fetchone()
            what = person["name"] if person else key
        elif table == "meddpicc":
            if key not in active_keys:
                continue
            what = methodology.label_for(key)
        elif table == "deal_risks":
            what = "risk: " + key.removeprefix("risk:").replace("_", " ")
        else:
            what = "deal health"
        c["what"], c["field_label"] = what, fld.replace("_", " ")
        c["reason"] = _loads(c["provenance"], {}).get("reason")
        out.append(c)
    return out


def latest_strategy(conn, deal_id, exclude_call=None, include_failed=False):
    """(strategy dict, artifact row) of the newest strategist artifact for the deal."""
    for row in conn.execute(
            "SELECT a.* FROM artifacts a JOIN calls c ON c.node_id=a.call_id "
            "WHERE c.deal_id=? AND a.kind='strategy' ORDER BY a.id DESC", (deal_id,)):
        if exclude_call and row["call_id"] == exclude_call:
            continue
        data = _loads(row["json"], {}) or {}
        if data.get("failed") and not include_failed:
            continue
        return data, row
    return None, None
