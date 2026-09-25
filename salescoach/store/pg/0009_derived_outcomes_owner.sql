-- Postgres migration 0009 (2026-09-25, security review: derived outcomes are one owner's). Hand-written; the
-- SQLite side is store/migrate.py migration 13 and the tracked salescoach/plugins/learning.sql, which
-- tests/test_schema_parity.py checks a database migrated through here against.
--
--   derived_outcomes   UNIQUE(kind, subject_type, subject_id) -> UNIQUE(owner_id, kind, subject_type, subject_id).
--                      learning/outcomes.recompute reads and writes only the acting user's rows; a row another
--                      user's recompute wrote about the same email, loop or call (a manager's interactive session
--                      read the team's rows before that fix) must neither block the owner's own upsert on a row
--                      the owner cannot see nor be overwritten by it. The manager's own next recompute prunes it.
--
-- Rows are untouched: every existing row already has its owner, and the new key is wider than the old.

ALTER TABLE derived_outcomes DROP CONSTRAINT IF EXISTS derived_outcomes_kind_subject_type_subject_id_key;
ALTER TABLE derived_outcomes ADD CONSTRAINT derived_outcomes_owner_id_kind_subject_type_subject_id_key
  UNIQUE (owner_id, kind, subject_type, subject_id);
