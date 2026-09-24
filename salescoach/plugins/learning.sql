-- Phase F1: learning layer. CREATE ... IF NOT EXISTS only; applied on every
-- stores.sales() connect. New columns on existing tables (deals.value, currency,
-- lost_reason) are added by salescoach/learning/__init__.py:ensure_columns().
--
-- Everything here is filled by code, by counting. No model writes to these tables.

-- Outcome ground truth: every stage/status change the user makes on a deal.
CREATE TABLE IF NOT EXISTS deal_stage_history (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id     TEXT NOT NULL,
  from_stage  TEXT,
  to_stage    TEXT,
  from_status TEXT,
  to_status   TEXT,
  lost_reason TEXT,
  changed_at  TEXT NOT NULL,
  "by"        TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_deal_stage_history_owner_id ON deal_stage_history(owner_id);
CREATE INDEX IF NOT EXISTS idx_stage_hist_deal ON deal_stage_history(deal_id, id);

-- Outcomes derived from facts already in the store (outcomes.recompute).
--   value: 1 = happened, 0 = did not, NULL = too early to say (window still open)
CREATE TABLE IF NOT EXISTS derived_outcomes (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,            -- email_replied|meeting_after_email|loop_closed_on_time|call_advanced
  subject_type TEXT NOT NULL,            -- email|loop|call
  subject_id   TEXT NOT NULL,
  deal_id      TEXT,
  value        INTEGER,
  computed_at  TEXT NOT NULL,
  details      TEXT NOT NULL DEFAULT '{}',
  owner_id TEXT NOT NULL DEFAULT 'local',
  UNIQUE(kind, subject_type, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_derived_outcomes_owner_id ON derived_outcomes(owner_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_deal ON derived_outcomes(deal_id, kind);

-- One row per (family, key) seen on one subject (a call, an email edit, a sent
-- nudge, a live nudge, a stakeholder). UNIQUE(family, key, subject) makes the
-- sync idempotent. `excluded` is the user's "Wrong"; a sync never clears it.
CREATE TABLE IF NOT EXISTS pattern_observations (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  family           TEXT NOT NULL,        -- seller|seller_series|email_voice|followup|nudge_trigger|persona|objection
  key              TEXT NOT NULL,
  subject          TEXT NOT NULL,        -- call:<id> | edit:<id> | email:<id> | nudge:<id> | person:<deal>:<id>
  polarity         TEXT,
  call_id          TEXT,
  deal_id          TEXT,
  email_id         INTEGER,
  nudge_id         INTEGER,
  evidence         TEXT NOT NULL DEFAULT '{}',   -- ids, turn numbers, counts; never email or transcript text
  confidence       TEXT,
  value            REAL,                 -- seller_series only
  outcome_kind     TEXT,
  outcome_value    TEXT,
  seller_id        TEXT,
  source_is_replay INTEGER NOT NULL DEFAULT 0,
  excluded         INTEGER NOT NULL DEFAULT 0,
  observed_at      TEXT,
  created_at       TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local',
  UNIQUE(family, key, subject)
);
CREATE INDEX IF NOT EXISTS idx_pattern_observations_owner_id ON pattern_observations(owner_id);
CREATE INDEX IF NOT EXISTS idx_pobs_family ON pattern_observations(family, key);
CREATE INDEX IF NOT EXISTS idx_pobs_call   ON pattern_observations(call_id);

-- What the coach believes. user_state, merged_into and no_prompt sit behind the
-- memory gate (user_input); patterns.recompute never writes them.
CREATE TABLE IF NOT EXISTS learned_patterns (
  id          TEXT PRIMARY KEY,          -- lp:<family>:u:<owner_id>:<key> (scope is always 'global' today)
  family      TEXT NOT NULL,
  key         TEXT NOT NULL,
  scope       TEXT NOT NULL DEFAULT 'global',
  polarity    TEXT,
  n_obs       INTEGER NOT NULL DEFAULT 0,
  n_calls     INTEGER NOT NULL DEFAULT 0,
  n_deals     INTEGER NOT NULL DEFAULT 0,
  support     INTEGER NOT NULL DEFAULT 0,   -- units in the current window that carry the pattern (a count)
  label       TEXT NOT NULL DEFAULT '',     -- ''|emerging|established
  status      TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','active','dormant','retired')),
  user_state  TEXT CHECK(user_state IS NULL OR user_state IN ('confirmed','wrong','retired')),
  merged_into TEXT,
  first_seen  TEXT,
  last_seen   TEXT,
  returned    INTEGER NOT NULL DEFAULT 0,
  summary     TEXT,
  stats       TEXT NOT NULL DEFAULT '{}',   -- family-specific counts (window size, outcome counts, a rate at n>=20)
  seller_id   TEXT,
  updated_at  TEXT,
  no_prompt   INTEGER NOT NULL DEFAULT 0,   -- the user's "Do not use in prompts" (phase F2); for_prompt honours it
  owner_id    TEXT NOT NULL DEFAULT 'local',
  UNIQUE(owner_id, family, key, scope)
);
CREATE INDEX IF NOT EXISTS idx_learned_patterns_owner_id ON learned_patterns(owner_id);
CREATE INDEX IF NOT EXISTS idx_lp_family ON learned_patterns(family, status);

-- Things the learner suggests and only the user can apply: tag merges and
-- live-coach trigger weights. Never applied automatically. At most ONE open
-- proposal per subject (the partial index below); decided ones stay as history,
-- and a new one for the same subject is opened only when the evidence has grown
-- to `proposals.repropose_factor` x decided_n (the n when the user decided).
-- A table made by phase F1 (UNIQUE(kind, subject), no decided_n) is rebuilt by
-- salescoach/learning/__init__.py:ensure_columns().
CREATE TABLE IF NOT EXISTS learning_proposals (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK(kind IN ('merge','trigger_weight')),
  subject     TEXT NOT NULL,
  pattern_id  TEXT,
  target_id   TEXT,
  summary     TEXT NOT NULL,
  payload     TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','accepted','dismissed')),
  applied     TEXT,                         -- config|recorded|merge
  created_at  TEXT NOT NULL,
  resolved_at TEXT,
  decided_n   INTEGER,                       -- evidence n (shown nudges / calls with the tag) when the user decided
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_learning_proposals_owner_id ON learning_proposals(owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lprop_open ON learning_proposals(kind, subject) WHERE status='open';
