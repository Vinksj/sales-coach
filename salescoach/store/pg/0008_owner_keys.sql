-- Postgres migration 0008 (2026-09-25, Phase 8: the last two keys two reps could collide on). Hand-written;
-- the SQLite side is store/migrate.py migration 12 and the tracked salescoach/plugins/learning.sql, which
-- tests/test_schema_parity.py checks a database migrated through here against.
--
--   pattern_observations   UNIQUE(family, key, subject) -> UNIQUE(owner_id, family, key, subject): one
--                          rep's observation must never block or overwrite another's (the sync of a
--                          manager who also sells saw the team's rows through the read policy).
--   learning_proposals     the one-open-proposal-per-subject index idx_lprop_open (kind, subject) ->
--                          (owner_id, kind, subject): a proposal subject is a tag or a trigger name, which
--                          every rep shares, so rep A's open "merge new:x" kept rep B's from opening.
--
-- Rows are untouched: every existing row already has its owner, and the new keys are wider than the old.

ALTER TABLE pattern_observations DROP CONSTRAINT IF EXISTS pattern_observations_family_key_subject_key;
ALTER TABLE pattern_observations ADD CONSTRAINT pattern_observations_owner_id_family_key_subject_key
  UNIQUE (owner_id, family, key, subject);

DROP INDEX IF EXISTS idx_lprop_open;
CREATE UNIQUE INDEX idx_lprop_open ON learning_proposals(owner_id, kind, subject) WHERE status='open';
