"""Which tables belong to whom, ahead of multi-user.

Every table the schema creates is classified here, and tests/isolation/test_catalog_lint.py fails
the build when one is not: a table nobody has thought about cannot reach a multi-user database.
No owner column exists yet (Phase 1 adds `owner_id` to every OWNED table; Phase 2 puts row-level
security on it); this file is the list that work will run down.

  OWNED   one rep's work. Reads and writes are scoped to the owner; a manager reads, never writes.
  ORG     the shared directory: visible to everyone in the org, so that a hidden row can never
          collide with a UNIQUE key (people.email) and every rep sees the same company and contact.
  SYSTEM  the machinery, never shown as anyone's data: the bus, small key-value facts, migrations.

Decisions worth a line:
  * calendar_cache and calendar_meetings are OWNED: a calendar is one rep's, and Phase 1 re-keys
    them on (owner_id, key) / (owner_id, event_id).
  * coach_reports, the learning tables and the seller memory are OWNED: coaching is about one
    seller, and team roll-ups are computed at read time, never stored (plan §Approach 5).
  * embeddings are OWNED because the text they index is.
  * state is SYSTEM for now; Phase 1 splits the per-user keys out into user_state.
  * schema_migrations exists on Postgres only (SQLite tracks its version in PRAGMA user_version).
"""

OWNED = "OWNED"
ORG = "ORG"
SYSTEM = "SYSTEM"

TABLE_CLASS = {
    # -- engine core
    "nodes": OWNED, "edges": OWNED, "events": OWNED, "sources": OWNED,
    # -- deal and relationship memory
    "accounts": ORG, "people": ORG,
    "deals": OWNED, "deal_people": OWNED,
    # -- transcript memory
    "calls": OWNED, "call_participants": OWNED, "turns": OWNED, "speakers": OWNED,
    # -- observability and agent output
    "agent_runs": OWNED, "artifacts": OWNED, "claims": OWNED, "assessments": OWNED, "reconciliations": OWNED,
    # -- loops, email, seller memory, memory gate
    "loops": OWNED, "emails": OWNED, "email_edits": OWNED,
    "seller_observations": OWNED, "seller_patterns": OWNED,
    "field_provenance": OWNED, "memory_conflicts": OWNED,
    # -- machinery
    "wf_events": SYSTEM, "state": SYSTEM, "schema_migrations": SYSTEM,
    # -- execution plugin
    "followup_decisions": OWNED, "email_replies": OWNED, "reply_proposals": OWNED,
    "calendar_cache": OWNED, "calendar_meetings": OWNED, "slot_fills": OWNED, "autosend_log": OWNED,
    # -- intelligence plugin
    "stakeholders": OWNED, "meddpicc": OWNED, "deal_risks": OWNED, "deal_health": OWNED,
    "deal_health_history": OWNED, "coach_reports": OWNED, "prep_briefs": OWNED, "embeddings": OWNED,
    # -- learning plugin
    "deal_stage_history": OWNED, "derived_outcomes": OWNED, "pattern_observations": OWNED,
    "learned_patterns": OWNED, "learning_proposals": OWNED,
    # -- live coach plugin
    "nudges": OWNED, "coach_state": OWNED,
}

# Tables that exist on one backend only, and why.
POSTGRES_ONLY = {"schema_migrations": "migration ledger; SQLite uses PRAGMA user_version"}
SQLITE_ONLY = {"sqlite_sequence": "SQLite's AUTOINCREMENT bookkeeping"}


def tables_of(kind: str) -> frozenset:
    return frozenset(t for t, k in TABLE_CLASS.items() if k == kind)
