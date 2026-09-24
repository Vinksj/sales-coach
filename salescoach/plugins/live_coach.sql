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
  created_at        TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_nudges_owner_id ON nudges(owner_id);
CREATE INDEX IF NOT EXISTS idx_nudges_call ON nudges(call_id, session, t_call);

-- Conversation-state snapshots (after each slow pass, periodically, and at the end).
CREATE TABLE IF NOT EXISTS coach_state (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id    TEXT NOT NULL,
  session    TEXT NOT NULL,
  t_call     REAL NOT NULL,
  json       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_coach_state_owner_id ON coach_state(owner_id);
CREATE INDEX IF NOT EXISTS idx_coach_state_call ON coach_state(call_id, session, t_call);
