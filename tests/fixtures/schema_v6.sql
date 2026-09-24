-- The tracked SQLite schema at user_version 6 (commit 4020ed4, before Phase 1), frozen for
-- tests/test_owner_columns.py: migration 7 must carry a database of this shape forward.

-- ==== salescoach/store/schema-sales.sql ====
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
  depth              TEXT DEFAULT 'full'
);

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
  FOREIGN KEY(src) REFERENCES nodes(id),
  FOREIGN KEY(dst) REFERENCES nodes(id)
);
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
  source_id TEXT
);
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
  FOREIGN KEY(node_id) REFERENCES nodes(id)
);

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
  lost_reason  TEXT
);

CREATE TABLE IF NOT EXISTS people (
  node_id          TEXT PRIMARY KEY REFERENCES nodes(id),
  name             TEXT NOT NULL,
  email            TEXT UNIQUE,
  account_id       TEXT REFERENCES nodes(id),
  title            TEXT,
  is_me            INTEGER NOT NULL DEFAULT 0,
  contact_file     TEXT,                    -- ~/.claude/contacts/<name>.md
  world_contact_id TEXT
);

CREATE TABLE IF NOT EXISTS deal_people (
  deal_id      TEXT NOT NULL REFERENCES nodes(id),
  person_id    TEXT NOT NULL REFERENCES nodes(id),
  role_in_deal TEXT,
  PRIMARY KEY (deal_id, person_id)
);

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
  history        INTEGER NOT NULL DEFAULT 0 -- 1 = backfilled history: analysed, never a drafted follow-up, not claimed for Jarvis
);
CREATE INDEX IF NOT EXISTS idx_calls_deal  ON calls(deal_id);
CREATE INDEX IF NOT EXISTS idx_calls_state ON calls(wf_state);

CREATE TABLE IF NOT EXISTS call_participants (
  call_id   TEXT NOT NULL REFERENCES nodes(id),
  person_id TEXT NOT NULL REFERENCES nodes(id),
  PRIMARY KEY (call_id, person_id)
);

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
  PRIMARY KEY (call_id, tier, idx)
);

CREATE TABLE IF NOT EXISTS speakers (
  call_id   TEXT NOT NULL REFERENCES nodes(id),
  cluster   TEXT NOT NULL,
  channel   TEXT NOT NULL,
  person_id TEXT,
  embedding BLOB,
  PRIMARY KEY (call_id, cluster)
);

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
  started_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_call ON agent_runs(call_id);

CREATE TABLE IF NOT EXISTS artifacts (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id        TEXT NOT NULL REFERENCES nodes(id),
  kind           TEXT NOT NULL,             -- quality|summary|analysis|actions|email
  run_id         INTEGER REFERENCES agent_runs(id),
  input_sha      TEXT,
  prompt_version TEXT,
  json           TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
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
  created_at     TEXT NOT NULL
);
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
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  subject        TEXT NOT NULL,
  verdict        TEXT NOT NULL,
  rationale      TEXT,
  assessment_ids TEXT NOT NULL DEFAULT '[]',
  created_at     TEXT NOT NULL
);

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
  closed_at           TEXT
);
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
  updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS email_edits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id   INTEGER NOT NULL REFERENCES emails(id),
  draft_body TEXT,
  final_body TEXT,
  created_at TEXT NOT NULL
);

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
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_tag ON seller_observations(tag);

CREATE TABLE IF NOT EXISTS seller_patterns (
  tag                      TEXT PRIMARY KEY,
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
  updated_at               TEXT
);

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
  PRIMARY KEY (entity_id, field)
);

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
  resolved_at         TEXT
);

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

CREATE TABLE IF NOT EXISTS state (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);

-- ==== salescoach/plugins/execution.sql ====
-- Phase 4, agentic execution. CREATE ... IF NOT EXISTS only: stores.sales()
-- re-applies this file on every connect.

-- Every follow-up decision, deterministic or agent-made, so "why did the
-- system nudge (or not nudge) this person?" always has an answer.
CREATE TABLE IF NOT EXISTS followup_decisions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  loop_id           TEXT NOT NULL,
  deal_id           TEXT,
  eval_date         TEXT NOT NULL,              -- IST date the evaluation ran for
  stage             TEXT NOT NULL CHECK(stage IN ('rules','agent','user')),
  check_name        TEXT,                       -- which deterministic check decided, or 'agent'
  decision          TEXT NOT NULL CHECK(decision IN
                      ('send_nudge','wait_until','escalate','close_as_stale','ask_user','skip')),
  rationale         TEXT NOT NULL,
  relationship_risk TEXT,
  relationship_note TEXT,
  wait_until        TEXT,
  next_check_at     TEXT,
  facts             TEXT NOT NULL DEFAULT '{}', -- what the decision saw
  run_id            INTEGER,                    -- agent_runs row for the decision
  nudge_run_id      INTEGER,
  email_id          INTEGER,                    -- the nudge drafted for it
  risk_loop_id      TEXT,
  conflict_id       INTEGER,
  sent_counted_at   TEXT,                       -- when EMAIL_SENT for its nudge was counted
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fud_loop  ON followup_decisions(loop_id, eval_date);
CREATE INDEX IF NOT EXISTS idx_fud_email ON followup_decisions(email_id);

-- Buyer replies in the Gmail threads of emails we sent. Bodies are untrusted
-- text: parsed and quoted, never followed.
CREATE TABLE IF NOT EXISTS email_replies (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id   TEXT NOT NULL UNIQUE,
  thread_id    TEXT NOT NULL,
  email_id     INTEGER,                         -- our sent email in that thread
  deal_id      TEXT,
  person_id    TEXT,
  from_addr    TEXT NOT NULL,
  from_name    TEXT,
  subject      TEXT,
  received_at  TEXT NOT NULL,
  body         TEXT NOT NULL,                   -- the new text, quoted history stripped
  body_full    TEXT,
  status       TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','analyzed','failed','reviewed')),
  summary      TEXT,
  needs_user   INTEGER NOT NULL DEFAULT 0,
  ignored_instructions TEXT NOT NULL DEFAULT '[]',   -- text in the reply aimed at "the assistant", not acted on
  run_id       INTEGER,
  error        TEXT,
  created_at   TEXT NOT NULL,
  reviewed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_replies_deal   ON email_replies(deal_id, received_at);
CREATE INDEX IF NOT EXISTS idx_replies_thread ON email_replies(thread_id);

-- What the reply-analysis agent said about each open loop, after validation.
CREATE TABLE IF NOT EXISTS reply_proposals (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  reply_id         INTEGER NOT NULL,
  loop_id          TEXT,
  verdict          TEXT NOT NULL,               -- done|waiting|superseded|new_commitment
  statement        TEXT,
  quote            TEXT,
  paragraphs       TEXT NOT NULL DEFAULT '[]',
  model_confidence TEXT,
  confidence       TEXT,                        -- after the evidence check
  evidence_found   INTEGER NOT NULL DEFAULT 0,
  notes            TEXT NOT NULL DEFAULT '[]',
  outcome          TEXT,                        -- applied|conflict|needs_review|unchanged|created|rejected
  conflict_id      INTEGER,
  new_loop_id      TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rprop_reply ON reply_proposals(reply_id);

-- Calendar reads (read-only) and the deal meetings found in them.
CREATE TABLE IF NOT EXISTS calendar_cache (
  key        TEXT PRIMARY KEY,
  fetched_at TEXT NOT NULL,
  source     TEXT,
  events     TEXT NOT NULL DEFAULT '[]',
  error      TEXT
);

CREATE TABLE IF NOT EXISTS calendar_meetings (
  event_id        TEXT PRIMARY KEY,
  deal_id         TEXT,
  title           TEXT,
  start_at        TEXT,
  end_at          TEXT,
  attendees       TEXT NOT NULL DEFAULT '[]',
  matched_domains TEXT NOT NULL DEFAULT '[]',
  first_seen_at   TEXT NOT NULL,
  updated_at      TEXT,
  prep_status     TEXT NOT NULL DEFAULT 'pending'
                  CHECK(prep_status IN ('pending','ready','unavailable','failed')),
  prep_ref        TEXT,
  prep_error      TEXT,
  record          TEXT NOT NULL DEFAULT 'no',   -- yes: start recording at the meeting's time
  call_id         TEXT,                         -- the capture made for it
  meeting_url     TEXT,                         -- join link
  last_seen_at    TEXT,                         -- last calendar sync that still listed it
  record_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_calmeet_start ON calendar_meetings(start_at);

-- Every attempt to fill [SLOTS] from the calendar, verified or not.
CREATE TABLE IF NOT EXISTS slot_fills (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id        INTEGER NOT NULL,
  status          TEXT NOT NULL CHECK(status IN ('filled','unavailable','no_slots','not_needed','refused')),
  slots           TEXT NOT NULL DEFAULT '[]',
  sentence        TEXT,
  reason          TEXT,
  calendar_source TEXT,
  busy_count      INTEGER,
  verified_at     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slotfill_email ON slot_fills(email_id);

-- Every auto-send evaluation that changed outcome, including dry runs.
CREATE TABLE IF NOT EXISTS autosend_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id   INTEGER NOT NULL,
  policy     TEXT,
  outcome    TEXT NOT NULL CHECK(outcome IN ('refused','would_send','sent','failed')),
  reasons    TEXT NOT NULL DEFAULT '[]',
  dry_run    INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autosend_email ON autosend_log(email_id, id);

-- ==== salescoach/plugins/intelligence.sql ====
-- Phase 3: sales intelligence tables. CREATE ... IF NOT EXISTS only; applied on
-- every stores.sales() connect.
--
-- stakeholders / meddpicc / deal_risks / deal_health sit behind the memory gate
-- (gate.register_table in intel/tables.py). Their keys are single surrogate
-- columns because the gate addresses one entity id per row:
--   stakeholders.id = <deal_id>:<person_id>
--   meddpicc.id     = <deal_id>:<element>
--   deal_risks.id   = <deal_id>:<type>
--   deal_health.id  = <deal_id>:health
-- The confidence and provenance of each gated field's current value live in
-- field_provenance; the seller's edits are user_input and are never overwritten.

CREATE TABLE IF NOT EXISTS stakeholders (
  id                    TEXT PRIMARY KEY,
  deal_id               TEXT NOT NULL,
  person_id             TEXT NOT NULL,
  role                  TEXT,
  influence             TEXT,              -- high|medium|low|unknown
  incentives            TEXT,              -- json list
  concerns              TEXT,              -- json list
  relationship_strength TEXT,              -- strong|moderate|weak|none|unknown
  position              TEXT,              -- champion|supporter|neutral|skeptic|blocker|unknown
  champion_potential    TEXT,              -- high|medium|low|none|unknown
  ability_to_block      TEXT,              -- high|medium|low|unknown
  evidence              TEXT NOT NULL DEFAULT '[]',
  confidence            TEXT,              -- of the latest strategist read
  run_id                INTEGER,
  updated_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_stakeholders_deal ON stakeholders(deal_id);

CREATE TABLE IF NOT EXISTS meddpicc (
  id            TEXT PRIMARY KEY,
  deal_id       TEXT NOT NULL,
  element       TEXT NOT NULL,             -- metrics|economic_buyer|decision_criteria|decision_process|paper_process|identify_pain|champion|competition
  status        TEXT,                      -- known|partial|unknown
  what_we_know  TEXT,
  gap           TEXT,
  next_question TEXT,
  evidence      TEXT NOT NULL DEFAULT '[]',
  confidence    TEXT,
  run_id        INTEGER,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_meddpicc_deal ON meddpicc(deal_id);

CREATE TABLE IF NOT EXISTS deal_risks (
  id          TEXT PRIMARY KEY,
  deal_id     TEXT NOT NULL,
  type        TEXT NOT NULL,
  severity    TEXT,                        -- critical|high|medium|low
  status      TEXT,                        -- open|cleared|dismissed
  description TEXT,
  mitigation  TEXT,
  evidence    TEXT NOT NULL DEFAULT '[]',
  source      TEXT,                        -- strategist|rule
  run_id      INTEGER,
  first_seen  TEXT,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_risks_deal ON deal_risks(deal_id);

CREATE TABLE IF NOT EXISTS deal_health (
  id               TEXT PRIMARY KEY,
  deal_id          TEXT NOT NULL UNIQUE,
  score            INTEGER,
  label            TEXT,
  rationale        TEXT,
  model_score      INTEGER,                -- what the strategist said before caps
  caps             TEXT NOT NULL DEFAULT '[]',
  next_best_action TEXT,                   -- json
  summary          TEXT,
  call_id          TEXT,
  run_id           INTEGER,
  updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS deal_health_history (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id    TEXT NOT NULL,
  score      INTEGER,
  label      TEXT,
  call_id    TEXT,
  run_id     INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_hist_deal ON deal_health_history(deal_id, id);

CREATE TABLE IF NOT EXISTS coach_reports (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  calls_analysed INTEGER NOT NULL,
  trigger        TEXT,                     -- analysis|review|manual
  input_sha      TEXT,
  run_id         INTEGER,
  json           TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prep_briefs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id       TEXT NOT NULL,
  meeting_title TEXT,
  meeting_at    TEXT,
  attendees     TEXT NOT NULL DEFAULT '[]',
  run_id        INTEGER,
  json          TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prep_deal ON prep_briefs(deal_id, id);

-- Local embeddings (nomic-embed-text via Ollama on loopback). One row per
-- entity and model; text_sha detects when the source text changed.
CREATE TABLE IF NOT EXISTS embeddings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,               -- claim|insight|observation|window
  entity_id   TEXT NOT NULL,
  call_id     TEXT,
  deal_id     TEXT,
  text        TEXT NOT NULL,
  text_sha    TEXT NOT NULL,
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL,
  vector      BLOB NOT NULL,               -- float32
  created_at  TEXT NOT NULL,
  UNIQUE(entity_type, entity_id, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_type ON embeddings(model, entity_type);

-- ==== salescoach/plugins/learning.sql ====
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
  "by"        TEXT NOT NULL
);
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
  UNIQUE(kind, subject_type, subject_id)
);
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
  UNIQUE(family, key, subject)
);
CREATE INDEX IF NOT EXISTS idx_pobs_family ON pattern_observations(family, key);
CREATE INDEX IF NOT EXISTS idx_pobs_call   ON pattern_observations(call_id);

-- What the coach believes. user_state, merged_into and no_prompt sit behind the
-- memory gate (user_input); patterns.recompute never writes them.
CREATE TABLE IF NOT EXISTS learned_patterns (
  id          TEXT PRIMARY KEY,          -- lp:<family>:<scope>:<key>
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
  UNIQUE(family, key, scope)
);
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
  decided_n   INTEGER                       -- evidence n (shown nudges / calls with the tag) when the user decided
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lprop_open ON learning_proposals(kind, subject) WHERE status='open';

-- ==== salescoach/plugins/live_coach.sql ====
-- Phase 2 live coaching. Applied by stores.sales() on every connect, so
-- CREATE ... IF NOT EXISTS only.

-- Every candidate the coach produced, shown or not. Suppressed rows carry the
-- reason they never reached the screen; that is the tuning data. A session is
-- one coach run over a call: the live run, or any number of replays.
CREATE TABLE IF NOT EXISTS nudges (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id           TEXT NOT NULL,
  session           TEXT NOT NULL,
  mode              TEXT NOT NULL DEFAULT 'live' CHECK(mode IN ('live','replay')),
  t_call            REAL NOT NULL,          -- seconds since call start: shown at, or retired at
  raised_t          REAL,                   -- when the candidate was raised
  trigger           TEXT NOT NULL,
  text              TEXT NOT NULL,
  kind              TEXT NOT NULL CHECK(kind IN ('moment','state')),
  source            TEXT NOT NULL CHECK(source IN ('fast','slow')),
  score             REAL,
  confidence        REAL,
  shown             INTEGER NOT NULL DEFAULT 0,
  suppressed_reason TEXT,
  anchor_text       TEXT,
  anchor_t          REAL,
  entity            TEXT,
  rationale         TEXT,
  urgency           TEXT,
  dismissed         INTEGER NOT NULL DEFAULT 0,
  outcome           TEXT CHECK(outcome IN ('followed','ignored','unknown')),
  outcome_evidence  TEXT,
  shown_wall        TEXT,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nudges_call ON nudges(call_id, session, t_call);

-- Conversation-state snapshots (after each slow pass, periodically, and at the end).
CREATE TABLE IF NOT EXISTS coach_state (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id    TEXT NOT NULL,
  session    TEXT NOT NULL,
  t_call     REAL NOT NULL,
  json       TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coach_state_call ON coach_state(call_id, session, t_call);
