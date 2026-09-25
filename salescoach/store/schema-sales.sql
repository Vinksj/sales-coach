-- sales.db — the SELLER store, on the graph/event engine in store/engine.py
-- (brain.db curious · world.db operator · learn.db · kn.db · sales.db).
--
-- CONFIDENTIALITY: customer call transcripts, audio paths and deal state.
-- Lives in ~/.claude/sales-coach/data: outside brain-os (the nightly git
-- snapshot never sees it) and outside the iCloud Desktop. Never exported.
--
-- Memory layers (plan §Memory schema):
--   transcript memory  → calls, turns, speakers
--   episodic memory    → events (every mutation, before/after, source_id)
--   provenance         → sources
--   deal memory        → accounts, deals, deal_people
--   relationship mem.  → people (links to ~/.claude/contacts, never copies)
--   open-loop memory   → loops
--   seller memory      → seller_observations, seller_patterns

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---- Engine core: identical shape to world.db so brain-os/db.py drives it --
CREATE TABLE IF NOT EXISTS nodes (
  id                 TEXT PRIMARY KEY,
  type               TEXT NOT NULL,         -- account|deal|person|call|loop
  kind               TEXT,
  title              TEXT,
  standfirst         TEXT,
  lenses             TEXT,
  status             TEXT,
  confidence         REAL DEFAULT 0.5,
  created_at         TEXT,
  last_reinforced_at TEXT,
  decay_rate         REAL DEFAULT 0.0,
  body_ref           TEXT,
  depth              TEXT DEFAULT 'full',
  owner_id           TEXT DEFAULT 'local',   -- NULL only for account/person nodes (the org directory)
  CHECK(owner_id IS NOT NULL OR type IN ('account','person'))
);
CREATE INDEX IF NOT EXISTS idx_nodes_owner_id ON nodes(owner_id);

CREATE TABLE IF NOT EXISTS edges (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  src                TEXT NOT NULL,
  dst                TEXT NOT NULL,
  type               TEXT NOT NULL,
  confidence         REAL DEFAULT 0.5,
  evidence           TEXT,
  t_valid_from       TEXT,
  t_valid_until      TEXT,
  created_at         TEXT,
  last_reinforced_at TEXT,
  decay_rate         REAL DEFAULT 0.0,
  owner_id TEXT NOT NULL DEFAULT 'local',
  FOREIGN KEY(src) REFERENCES nodes(id),
  FOREIGN KEY(dst) REFERENCES nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_edges_owner_id ON edges(owner_id);
CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  actor     TEXT,
  kind      TEXT NOT NULL,
  node_id   TEXT,
  edge_id   INTEGER,
  before    TEXT,
  after     TEXT,
  source_id TEXT,
  owner_id TEXT NOT NULL DEFAULT 'local',
  actor_user_id TEXT                        -- the users row that made an admin change (Phase 3 audit)
);
CREATE INDEX IF NOT EXISTS idx_events_owner_id ON events(owner_id);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS sources (
  node_id  TEXT PRIMARY KEY,
  uri      TEXT,
  sha      TEXT,
  raw_path TEXT,
  capture  TEXT,                            -- capture|audio_file|granola|paste|user_input|world
  lineage  TEXT,
  owner_id TEXT NOT NULL DEFAULT 'local',
  FOREIGN KEY(node_id) REFERENCES nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_sources_owner_id ON sources(owner_id);

-- ---- Deal + relationship memory -------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
  node_id TEXT PRIMARY KEY REFERENCES nodes(id),
  name    TEXT NOT NULL,
  domains TEXT NOT NULL DEFAULT '[]'        -- json array of email domains
);

CREATE TABLE IF NOT EXISTS deals (
  node_id      TEXT PRIMARY KEY REFERENCES nodes(id),
  account_id   TEXT REFERENCES nodes(id),
  name         TEXT NOT NULL,
  stage        TEXT,
  status       TEXT NOT NULL DEFAULT 'active'
               CHECK(status IN ('active','won','lost','paused')),
  next_step    TEXT,
  close_target TEXT,
  updated_at   TEXT,
  value        REAL,                        -- learning plugin (outcomes): added by learning.ensure_columns
  currency     TEXT,                        --   on a SQLite store created before these columns existed
  lost_reason  TEXT,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_deals_owner_id ON deals(owner_id);

CREATE TABLE IF NOT EXISTS people (
  node_id          TEXT PRIMARY KEY REFERENCES nodes(id),
  name             TEXT NOT NULL,
  email            TEXT UNIQUE,
  account_id       TEXT REFERENCES nodes(id),
  title            TEXT,
  is_me            INTEGER NOT NULL DEFAULT 0, -- derived: 1 iff user_id IS NOT NULL (any internal user)
  contact_file     TEXT,                    -- ~/.claude/contacts/<name>.md
  world_contact_id TEXT,
  user_id          TEXT UNIQUE               -- the users row this person IS; NULL for buyers
);

CREATE TABLE IF NOT EXISTS deal_people (
  deal_id      TEXT NOT NULL REFERENCES nodes(id),
  person_id    TEXT NOT NULL REFERENCES nodes(id),
  role_in_deal TEXT,
  owner_id TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (deal_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_deal_people_owner_id ON deal_people(owner_id);

-- ---- Transcript memory ----------------------------------------------------
CREATE TABLE IF NOT EXISTS calls (
  node_id        TEXT PRIMARY KEY REFERENCES nodes(id),
  deal_id        TEXT REFERENCES nodes(id),
  source         TEXT NOT NULL,            -- capture|audio_file|paste|granola|upload|folder|webhook|fireflies|fathom|...
  source_ref     TEXT UNIQUE,              -- '<kind>:<id at the source>': importing the same thing twice is a no-op
  title          TEXT,
  started_at     TEXT,
  ended_at       TEXT,
  audio_dir      TEXT,
  lang_mode      TEXT NOT NULL DEFAULT 'auto' CHECK(lang_mode IN ('en','hinglish','auto')),
  asr_live_model  TEXT,
  asr_final_model TEXT,
  transcript_sha TEXT,
  quality_score  REAL,
  wf_state       TEXT NOT NULL DEFAULT 'live',
  wf_error       TEXT,
  updated_at     TEXT,
  history        INTEGER NOT NULL DEFAULT 0, -- 1 = backfilled history: analysed, never a drafted follow-up, not claimed for Jarvis
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_calls_owner_id ON calls(owner_id);
CREATE INDEX IF NOT EXISTS idx_calls_deal  ON calls(deal_id);
CREATE INDEX IF NOT EXISTS idx_calls_state ON calls(wf_state);

CREATE TABLE IF NOT EXISTS call_participants (
  call_id   TEXT NOT NULL REFERENCES nodes(id),
  person_id TEXT NOT NULL REFERENCES nodes(id),
  owner_id TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (call_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_call_participants_owner_id ON call_participants(owner_id);

CREATE TABLE IF NOT EXISTS turns (
  call_id         TEXT NOT NULL REFERENCES nodes(id),
  tier            TEXT NOT NULL CHECK(tier IN ('live','final')),
  idx             INTEGER NOT NULL,
  channel         TEXT NOT NULL CHECK(channel IN ('me','them')),
  speaker_cluster TEXT,
  person_id       TEXT,
  t_start         REAL,                     -- seconds from call start
  t_end           REAL,
  text            TEXT NOT NULL,
  asr_logprob     REAL,
  no_speech_prob  REAL,
  quality         TEXT CHECK(quality IN ('ok','partial','garbled')),
  quality_note    TEXT,
  bleed_flag      INTEGER NOT NULL DEFAULT 0,
  owner_id TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (call_id, tier, idx)
);
CREATE INDEX IF NOT EXISTS idx_turns_owner_id ON turns(owner_id);

CREATE TABLE IF NOT EXISTS speakers (
  call_id   TEXT NOT NULL REFERENCES nodes(id),
  cluster   TEXT NOT NULL,
  channel   TEXT NOT NULL,
  person_id TEXT,
  embedding BLOB,
  owner_id TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (call_id, cluster)
);
CREATE INDEX IF NOT EXISTS idx_speakers_owner_id ON speakers(owner_id);

-- ---- Observability: every agent execution ---------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  agent          TEXT NOT NULL,
  call_id        TEXT,
  prompt_version TEXT,
  provider       TEXT,
  model          TEXT,
  isolation      TEXT,                      -- clean|guarded|n/a
  input_refs     TEXT NOT NULL DEFAULT '{}',
  input_sha      TEXT,
  output         TEXT,
  status         TEXT NOT NULL CHECK(status IN ('running','ok','invalid','error')),
  error          TEXT,
  duration_ms    INTEGER,
  cost_usd       REAL,
  created_items  TEXT NOT NULL DEFAULT '[]',
  rejected_items TEXT NOT NULL DEFAULT '[]',
  started_at     TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_owner_id ON agent_runs(owner_id);
CREATE INDEX IF NOT EXISTS idx_runs_call ON agent_runs(call_id);

CREATE TABLE IF NOT EXISTS artifacts (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id        TEXT NOT NULL REFERENCES nodes(id),
  kind           TEXT NOT NULL,             -- quality|summary|analysis|actions|email
  run_id         INTEGER REFERENCES agent_runs(id),
  input_sha      TEXT,
  prompt_version TEXT,
  json           TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_artifacts_owner_id ON artifacts(owner_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_call ON artifacts(call_id, kind);

-- ---- Facts vs inferences, and agent disagreement --------------------------
CREATE TABLE IF NOT EXISTS claims (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id        TEXT REFERENCES nodes(id),
  deal_id        TEXT,
  agent          TEXT NOT NULL,
  subject        TEXT NOT NULL,
  statement      TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK(kind IN ('fact','inference','assumption')),
  confidence     TEXT NOT NULL CHECK(confidence IN ('explicit','high','medium','low')),
  evidence_turns TEXT NOT NULL DEFAULT '[]',
  evidence_quote TEXT,
  created_at     TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_claims_owner_id ON claims(owner_id);
CREATE INDEX IF NOT EXISTS idx_claims_deal ON claims(deal_id, subject);

CREATE TABLE IF NOT EXISTS assessments (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  subject        TEXT NOT NULL,             -- e.g. deal:<id>/momentum
  call_id        TEXT,
  agent          TEXT NOT NULL,
  stance         TEXT NOT NULL,
  confidence     TEXT,
  rationale      TEXT,
  evidence_turns TEXT NOT NULL DEFAULT '[]',
  created_at     TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_assessments_owner_id ON assessments(owner_id);

CREATE TABLE IF NOT EXISTS reconciliations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  subject        TEXT NOT NULL,
  verdict        TEXT NOT NULL,
  rationale      TEXT,
  assessment_ids TEXT NOT NULL DEFAULT '[]',
  created_at     TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_reconciliations_owner_id ON reconciliations(owner_id);

-- ---- Open-loop memory -----------------------------------------------------
CREATE TABLE IF NOT EXISTS loops (
  node_id             TEXT PRIMARY KEY REFERENCES nodes(id),
  deal_id             TEXT REFERENCES nodes(id),
  call_id             TEXT REFERENCES nodes(id),
  type                TEXT NOT NULL CHECK(type IN
                        ('my_action','prospect_action','mutual','follow_up','info_request','deal_risk')),
  description         TEXT NOT NULL,
  owner               TEXT NOT NULL CHECK(owner IN ('me','prospect','mutual','internal')),
  owner_name          TEXT,
  owner_person_id     TEXT,
  source              TEXT NOT NULL CHECK(source IN
                        ('explicit_commitment','implied_commitment','recommended','user_input','world')),
  confidence          TEXT NOT NULL CHECK(confidence IN ('explicit','high','medium','low')),
  evidence_quote      TEXT,
  evidence_turns      TEXT NOT NULL DEFAULT '[]',
  priority            TEXT NOT NULL DEFAULT 'medium' CHECK(priority IN ('critical','high','medium','low')),
  due_date            TEXT,
  due_date_confidence TEXT NOT NULL DEFAULT 'unknown' CHECK(due_date_confidence IN ('explicit','inferred','unknown')),
  status              TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','waiting','done','cancelled','superseded')),
  superseded_by       TEXT,
  follow_up_required  INTEGER NOT NULL DEFAULT 0,
  follow_up_strategy  TEXT,
  next_check_at       TEXT,
  follow_up_count     INTEGER NOT NULL DEFAULT 0,
  escalation          TEXT,
  dependencies        TEXT NOT NULL DEFAULT '[]',
  review_state        TEXT NOT NULL DEFAULT 'proposed'
                        CHECK(review_state IN ('proposed','confirmed','rejected')),
  world_commitment_id TEXT,
  world_link          TEXT,                 -- mirror (we landed it) | adopted (model linked it) | imported
  created_at          TEXT NOT NULL,
  last_activity_at    TEXT,
  closed_at           TEXT,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_loops_owner_id ON loops(owner_id);
CREATE INDEX IF NOT EXISTS idx_loops_deal     ON loops(deal_id, status);
CREATE INDEX IF NOT EXISTS idx_loops_owner    ON loops(owner, status);
CREATE INDEX IF NOT EXISTS idx_loops_due      ON loops(due_date);
CREATE INDEX IF NOT EXISTS idx_loops_priority ON loops(priority);

-- ---- Outbound email -------------------------------------------------------
CREATE TABLE IF NOT EXISTS emails (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id          TEXT REFERENCES nodes(id),
  deal_id          TEXT REFERENCES nodes(id),
  kind             TEXT NOT NULL DEFAULT 'followup' CHECK(kind IN ('followup','nudge')),
  to_addrs         TEXT NOT NULL DEFAULT '[]',
  cc_addrs         TEXT NOT NULL DEFAULT '[]',
  subject          TEXT,
  body             TEXT,
  draft_body       TEXT,                    -- what the agent wrote; diffed against the sent body
  rationale        TEXT,
  version          INTEGER NOT NULL DEFAULT 1,
  status           TEXT NOT NULL DEFAULT 'drafted'
                   CHECK(status IN ('drafted','approved','sending','sent','saved_to_gmail','rejected','failed')),
  policy_decision  TEXT,
  lint             TEXT NOT NULL DEFAULT '[]',
  run_id           INTEGER,
  approved_at      TEXT,
  approved_by      TEXT,
  idempotency_key  TEXT UNIQUE,
  rfc822_message_id TEXT,                   -- deterministic per key, so Sent can be searched after a timeout
  attempt_mode     TEXT,                    -- send|draft: what the last approval tried, for recovery
  user_added_addrs TEXT NOT NULL DEFAULT '[]',
  gmail_message_id TEXT,
  gmail_thread_id  TEXT,
  gmail_draft_id   TEXT,
  sent_at          TEXT,
  error            TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_emails_owner_id ON emails(owner_id);

CREATE TABLE IF NOT EXISTS email_edits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id   INTEGER NOT NULL REFERENCES emails(id),
  draft_body TEXT,
  final_body TEXT,
  created_at TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_email_edits_owner_id ON email_edits(owner_id);

-- ---- Seller memory --------------------------------------------------------
CREATE TABLE IF NOT EXISTS seller_observations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id        TEXT NOT NULL REFERENCES nodes(id),
  tag            TEXT NOT NULL,
  polarity       TEXT NOT NULL CHECK(polarity IN ('weakness','strength')),
  severity       TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
  contexts       TEXT NOT NULL DEFAULT '[]',
  evidence_turns TEXT NOT NULL DEFAULT '[]',
  evidence_quote TEXT,
  confidence     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_seller_observations_owner_id ON seller_observations(owner_id);
CREATE INDEX IF NOT EXISTS idx_obs_tag ON seller_observations(tag);

CREATE TABLE IF NOT EXISTS seller_patterns (
  tag                      TEXT NOT NULL,
  name                     TEXT NOT NULL,
  description              TEXT,
  polarity                 TEXT NOT NULL CHECK(polarity IN ('weakness','strength')),
  frequency                REAL NOT NULL DEFAULT 0,
  calls_seen               INTEGER NOT NULL DEFAULT 0,
  calls_window             INTEGER NOT NULL DEFAULT 0,
  severity                 TEXT,
  contexts                 TEXT NOT NULL DEFAULT '[]',
  examples                 TEXT NOT NULL DEFAULT '[]',
  first_detected           TEXT,
  last_detected            TEXT,
  trend                    TEXT NOT NULL DEFAULT 'insufficient_data'
                           CHECK(trend IN ('improving','stable','worsening','insufficient_data')),
  recommended_intervention TEXT,
  status                   TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','active','retired')),
  updated_at               TEXT,
  owner_id                 TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (owner_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_seller_patterns_owner_id ON seller_patterns(owner_id);

-- ---- Memory gate ----------------------------------------------------------
-- The confidence and provenance behind each gated field's CURRENT value, so a
-- weaker later claim can never silently overwrite a stronger earlier one.
CREATE TABLE IF NOT EXISTS field_provenance (
  entity_id  TEXT NOT NULL,
  field      TEXT NOT NULL,
  value      TEXT,
  confidence TEXT NOT NULL,                 -- low|medium|high|explicit|user_input
  provenance TEXT NOT NULL DEFAULT '{}',    -- {"kind": call|user_input|email|world, "ref": ..., "turns": [...]}
  updated_at TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (entity_id, field)
);
CREATE INDEX IF NOT EXISTS idx_field_provenance_owner_id ON field_provenance(owner_id);

CREATE TABLE IF NOT EXISTS memory_conflicts (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id           TEXT NOT NULL,
  field               TEXT NOT NULL,
  existing_value      TEXT,
  existing_confidence TEXT,
  proposed_value      TEXT,
  proposed_confidence TEXT,
  provenance          TEXT,
  status              TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','accepted','rejected')),
  created_at          TEXT NOT NULL,
  resolved_at         TEXT,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_memory_conflicts_owner_id ON memory_conflicts(owner_id);

-- ---- Workflow bus ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS wf_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id     TEXT NOT NULL UNIQUE,
  type         TEXT NOT NULL,
  entity_id    TEXT,
  payload      TEXT NOT NULL DEFAULT '{}',
  causation_id TEXT,
  dedupe_key   TEXT UNIQUE,
  status       TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  error        TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT,
  not_before   TEXT                        -- NULL = claimable now; fail()/defer() set the earliest retry (UTC ISO)
);
CREATE INDEX IF NOT EXISTS idx_wf_status ON wf_events(status, id);

CREATE TABLE IF NOT EXISTS state (                -- org-wide facts; a user's own go in user_state
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);

-- ---- Users (salescoach/users.py) ------------------------------------------
-- The USER half of the old seller.yaml. The local install has one row, 'local',
-- whose profile is still read from seller.yaml; every other row is a cloud user.
CREATE TABLE IF NOT EXISTS users (
  id           TEXT PRIMARY KEY,
  email        TEXT UNIQUE,
  name         TEXT NOT NULL DEFAULT '',
  role         TEXT NOT NULL DEFAULT 'rep' CHECK(role IN ('rep','manager','admin')),
  team_id      TEXT,
  status       TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('invited','active','disabled')),
  google_sub   TEXT UNIQUE,
  extra_emails TEXT NOT NULL DEFAULT '[]',     -- json: addresses beyond `email`
  aliases      TEXT NOT NULL DEFAULT '[]',     -- json
  signature    TEXT,
  timezone     TEXT,
  languages    TEXT NOT NULL DEFAULT '[]',     -- json
  role_title   TEXT,
  style        TEXT,                           -- the user's style guide (style.md for the local user)
  call_context TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS teams (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_managers (
  team_id TEXT NOT NULL REFERENCES teams(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  PRIMARY KEY (team_id, user_id)
);

-- What `state` held for one user: automation bookkeeping, dismissed cards, per-user errors.
CREATE TABLE IF NOT EXISTS user_state (
  user_id    TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT,
  updated_at TEXT,
  PRIMARY KEY (user_id, key)
);

-- Speaker labels a user said were theirs (was sources.yaml me_labels, org-wide).
CREATE TABLE IF NOT EXISTS user_speaker_labels (
  user_id    TEXT NOT NULL,
  label_norm TEXT NOT NULL,                    -- sources.base.norm_label(label)
  label      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, label_norm)
);

-- ---- Sign-in, sessions and OAuth grants (Phase 3; salescoach/sessions.py, execution/tokens.py) ----
-- A browser holds a signed random session id; everything else about the session is here, so an
-- admin can end it (disable the user, "log out everywhere") and it stops at the next request.
CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id),
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,                 -- slides forward on use (sessions.SLIDING_DAYS)
  revoked_at   TEXT,
  ip           TEXT,
  user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- An invite is an allow-list entry: the admin typed an address; the app sends nothing. The users
-- row it creates carries status 'invited' until that address signs in with Google.
CREATE TABLE IF NOT EXISTS invites (
  email       TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id),
  invited_by  TEXT,
  created_at  TEXT NOT NULL,
  accepted_at TEXT
);

-- One live grant per (user, provider). Tokens are AES-256-GCM ciphertext under the key ring in
-- SALESCOACH_TOKEN_KEYS (execution/tokens.py); key_id says which key, so the ring can rotate.
CREATE TABLE IF NOT EXISTS oauth_tokens (
  user_id           TEXT NOT NULL REFERENCES users(id),
  provider          TEXT NOT NULL DEFAULT 'google',
  scopes            TEXT NOT NULL DEFAULT '[]',   -- json: every scope Google has granted so far
  refresh_token_enc TEXT,
  access_token_enc  TEXT,
  key_id            TEXT NOT NULL,
  expires_at        TEXT,                        -- of the cached access token
  email             TEXT,                        -- the Google account that consented
  status            TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','needs_reconsent','revoked')),
  last_error        TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT,
  PRIMARY KEY (user_id, provider)
);
