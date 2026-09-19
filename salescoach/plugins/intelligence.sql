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
