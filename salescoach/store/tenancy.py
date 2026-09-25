"""Which tables belong to whom, ahead of multi-user.

Every table the schema creates is classified here, and tests/isolation/test_catalog_lint.py fails
the build when one is not: a table nobody has thought about cannot reach a multi-user database.
Every OWNED table carries `owner_id` (Phase 1; the lint checks the column and its index); Phase 2
puts row-level security on it.

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
  * state is SYSTEM and org-wide; the per-user keys live in user_state (keyed on user_id, not
    owner_id: it is bookkeeping about a user, never a rep's work, so it is SYSTEM like state).
    user_speaker_labels likewise.
  * users, teams and team_managers are the directory of who is here: ORG, like accounts and people.
  * nodes.owner_id is NULL for account and person nodes (the shared directory's nodes) and NOT NULL
    for call, deal and loop nodes (a CHECK says so); the rows in accounts/people themselves are ORG.
  * schema_migrations and schema_repeatables exist on Postgres only (SQLite tracks its version in
    PRAGMA user_version and has no row-level security to re-apply).
  * sessions, invites and oauth_tokens (Phase 3) are SYSTEM: bookkeeping about a user (who is signed
    in, who may sign in, a user's Google grant), never a rep's work; store/rls.py polices each.
  * org_settings (Phase 6) is SYSTEM: the org's settings overlay, one row per settings file; raw_payloads
    is OWNED: what one user's source delivered (top-level: no parent, no trigger).
  * source_connections (Phase 4) is OWNED: a rep's own recorder account and its encrypted key. The
    rep's managers may read the row (status, last import), as for any OWNED row; nothing renders the
    ciphertext, and only a session bound to the owner decrypts it (sources/connections.py filters on
    owner_id = the actor as well). The push webhook resolves a connection's owner with nobody bound
    through app_source_connection_owner() (store/rls.py), an id, never content.
"""

OWNED = "OWNED"
ORG = "ORG"
SYSTEM = "SYSTEM"

TABLE_CLASS = {
    # -- engine core
    "nodes": OWNED, "edges": OWNED, "events": OWNED, "sources": OWNED,
    # -- the directory
    "accounts": ORG, "people": ORG, "users": ORG, "teams": ORG, "team_managers": ORG,
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
    "wf_events": SYSTEM, "state": SYSTEM, "user_state": SYSTEM, "user_speaker_labels": SYSTEM,
    "schema_migrations": SYSTEM, "schema_repeatables": SYSTEM,
    "sessions": SYSTEM, "invites": SYSTEM, "oauth_tokens": SYSTEM,    # Phase 3: about a user, never a rep's work
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
    # -- Phase 6: the org settings overlay is machinery; a raw payload is what one user's source delivered
    "org_settings": SYSTEM, "raw_payloads": OWNED,
    # -- Phase 4: one rep's own recorder account; what it delivers is that rep's
    "source_connections": OWNED,
}

# Tables that exist on one backend only, and why.
POSTGRES_ONLY = {"schema_migrations": "migration ledger; SQLite uses PRAGMA user_version",
                 "schema_repeatables": "checksums of the repeatable steps (store/pg/rls.sql); SQLite has no RLS"}
SQLITE_ONLY = {"sqlite_sequence": "SQLite's AUTOINCREMENT bookkeeping"}


# OWNED tables whose owner_id may be NULL, and why. Everything else OWNED is NOT NULL.
OWNER_NULLABLE = {"nodes": "account and person nodes belong to the org directory"}

# Child tables whose owner is the parent row's: on Postgres a BEFORE INSERT trigger copies it and
# refuses a mismatch (store/pg/0002_owner.sql). {child: ((parent_table, parent_key, child_column), ...)},
# first non-NULL child column wins. Tables not listed are top-level: their owner is the acting user.
OWNER_PARENTS = {
    "calls": (("nodes", "id", "node_id"),),
    "deals": (("nodes", "id", "node_id"),),
    "loops": (("nodes", "id", "node_id"), ("calls", "node_id", "call_id"), ("deals", "node_id", "deal_id")),
    "edges": (("nodes", "id", "src"),),
    "events": (("nodes", "id", "node_id"),),
    "sources": (("nodes", "id", "node_id"),),
    "deal_people": (("deals", "node_id", "deal_id"),),
    "call_participants": (("calls", "node_id", "call_id"),),
    "turns": (("calls", "node_id", "call_id"),),
    "speakers": (("calls", "node_id", "call_id"),),
    "agent_runs": (("calls", "node_id", "call_id"),),
    "artifacts": (("calls", "node_id", "call_id"),),
    "claims": (("calls", "node_id", "call_id"), ("deals", "node_id", "deal_id")),
    "assessments": (("calls", "node_id", "call_id"),),
    "emails": (("calls", "node_id", "call_id"), ("deals", "node_id", "deal_id")),
    "email_edits": (("emails", "id", "email_id"),),
    "seller_observations": (("calls", "node_id", "call_id"),),
    "followup_decisions": (("loops", "node_id", "loop_id"), ("deals", "node_id", "deal_id")),
    "email_replies": (("emails", "id", "email_id"),),
    "reply_proposals": (("email_replies", "id", "reply_id"),),
    "slot_fills": (("emails", "id", "email_id"),),
    "autosend_log": (("emails", "id", "email_id"),),
    "stakeholders": (("deals", "node_id", "deal_id"),),
    "meddpicc": (("deals", "node_id", "deal_id"),),
    "deal_risks": (("deals", "node_id", "deal_id"),),
    "deal_health": (("deals", "node_id", "deal_id"),),
    "deal_health_history": (("deals", "node_id", "deal_id"),),
    "prep_briefs": (("deals", "node_id", "deal_id"),),
    "embeddings": (("calls", "node_id", "call_id"), ("deals", "node_id", "deal_id")),
    "deal_stage_history": (("deals", "node_id", "deal_id"),),
    "derived_outcomes": (("deals", "node_id", "deal_id"),),
    "pattern_observations": (("calls", "node_id", "call_id"), ("deals", "node_id", "deal_id")),
    "nudges": (("calls", "node_id", "call_id"),),
    "coach_state": (("calls", "node_id", "call_id"),),
}


def tables_of(kind: str) -> frozenset:
    return frozenset(t for t, k in TABLE_CLASS.items() if k == kind)
