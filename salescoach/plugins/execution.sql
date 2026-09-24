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
  created_at        TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_followup_decisions_owner_id ON followup_decisions(owner_id);
CREATE INDEX IF NOT EXISTS idx_fud_loop  ON followup_decisions(loop_id, eval_date);
CREATE INDEX IF NOT EXISTS idx_fud_email ON followup_decisions(email_id);

-- Buyer replies in the Gmail threads of emails we sent. Bodies are untrusted
-- text: parsed and quoted, never followed.
CREATE TABLE IF NOT EXISTS email_replies (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id   TEXT NOT NULL,                   -- unique per owner (below): two mailboxes may see one message
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
  reviewed_at  TEXT,
  owner_id     TEXT NOT NULL DEFAULT 'local',
  UNIQUE(owner_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_email_replies_owner_id ON email_replies(owner_id);
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
  created_at       TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_reply_proposals_owner_id ON reply_proposals(owner_id);
CREATE INDEX IF NOT EXISTS idx_rprop_reply ON reply_proposals(reply_id);

-- Calendar reads (read-only) and the deal meetings found in them.
CREATE TABLE IF NOT EXISTS calendar_cache (
  key        TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  source     TEXT,
  events     TEXT NOT NULL DEFAULT '[]',
  error      TEXT,
  owner_id   TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (owner_id, key)
);
CREATE INDEX IF NOT EXISTS idx_calendar_cache_owner_id ON calendar_cache(owner_id);

CREATE TABLE IF NOT EXISTS calendar_meetings (
  event_id        TEXT NOT NULL,                -- unique per owner: two reps may share an invite
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
  record_error    TEXT,
  owner_id        TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (owner_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_calendar_meetings_owner_id ON calendar_meetings(owner_id);
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
  created_at      TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_slot_fills_owner_id ON slot_fills(owner_id);
CREATE INDEX IF NOT EXISTS idx_slotfill_email ON slot_fills(email_id);

-- Every auto-send evaluation that changed outcome, including dry runs.
CREATE TABLE IF NOT EXISTS autosend_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id   INTEGER NOT NULL,
  policy     TEXT,
  outcome    TEXT NOT NULL CHECK(outcome IN ('refused','would_send','sent','failed')),
  reasons    TEXT NOT NULL DEFAULT '[]',
  dry_run    INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  owner_id TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_autosend_log_owner_id ON autosend_log(owner_id);
CREATE INDEX IF NOT EXISTS idx_autosend_email ON autosend_log(email_id, id);
