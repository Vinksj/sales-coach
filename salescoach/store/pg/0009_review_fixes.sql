-- Postgres migration 0009 (2026-09-25, security review fixes). Hand-written; the
-- SQLite side is store/migrate.py migration 13 and the tracked salescoach/plugins/learning.sql, which
-- tests/test_schema_parity.py checks a database migrated through here against.
--
--   derived_outcomes   UNIQUE(kind, subject_type, subject_id) -> UNIQUE(owner_id, kind, subject_type, subject_id).
--                      learning/outcomes.recompute reads and writes only the acting user's rows; a row another
--                      user's recompute wrote about the same email, loop or call (a manager's interactive session
--                      read the team's rows before that fix) must neither block the owner's own upsert on a row
--                      the owner cannot see nor be overwritten by it. The manager's own next recompute prunes it.
--
--   access_log         entity_type CHECK ('call','deal') -> ('call','deal','email','coaching'): a manager's read of
--                      a rep's follow-up nudge (email) and of a rep's Coach / Learning pages (coaching, entity_id
--                      = the rep's user id) is logged too (manager/views.READ_PATHS, REP_PAGES). The insert
--                      policy's app_entity_owner() already answers for both kinds.
--   calendar_meetings  + accepted (INTEGER NOT NULL DEFAULT 1): the owner organised or accepted the meeting. Only then
--                      does a deal meeting ask for a prep brief (a paid model run); a bare invitation is shown only.
--
-- Rows are untouched: every existing row already has its owner, the new key is wider than the old, and the
-- new CHECK allows every value the old one did.

ALTER TABLE derived_outcomes DROP CONSTRAINT IF EXISTS derived_outcomes_kind_subject_type_subject_id_key;
ALTER TABLE derived_outcomes ADD CONSTRAINT derived_outcomes_owner_id_kind_subject_type_subject_id_key
  UNIQUE (owner_id, kind, subject_type, subject_id);

ALTER TABLE access_log DROP CONSTRAINT IF EXISTS access_log_entity_type_check;
ALTER TABLE access_log ADD CONSTRAINT access_log_entity_type_check
  CHECK (entity_type IN ('call','deal','email','coaching'));

ALTER TABLE calendar_meetings ADD COLUMN IF NOT EXISTS accepted INTEGER NOT NULL DEFAULT 1;
