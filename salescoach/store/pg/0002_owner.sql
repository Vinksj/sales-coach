-- Postgres migration 0002 (2026-09-24): identity and ownership. Hand-written; the SQLite side is
-- store/migrate.py migration 7 and the tracked files store/schema-sales.sql + plugins/*.sql, which
-- tests/test_schema_parity.py checks a database migrated through here against.
--
-- Every OWNED table (store/tenancy.py) gains owner_id, backfilled to 'local' (the one seller a
-- database had before today) and thereafter defaulting to the session setting app.user_id that
-- identity.bind() sets: NULLIF(current_setting('app.user_id', true), '') is NULL when nothing bound
-- an actor, so an owned INSERT with no acting user fails its NOT NULL instead of landing as nobody's.
-- Child rows take their parent's owner through app_child_owner() (below) and a mismatch is refused.
-- The keys two users would collide on are re-scoped: seller_patterns (owner_id, tag), calendar_cache
-- (owner_id, key), calendar_meetings (owner_id, event_id), email_replies UNIQUE(owner_id, message_id),
-- learned_patterns UNIQUE(owner_id, family, key, scope) with ids lp:<family>:u:<owner>:<key>.
-- Row-level security comes in 0003 (Phase 2); nothing here restricts a read.

-- ---- owner_id on every OWNED table --------------------------------------------------------
ALTER TABLE agent_runs ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE agent_runs ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_agent_runs_owner_id ON agent_runs(owner_id);
ALTER TABLE artifacts ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE artifacts ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_artifacts_owner_id ON artifacts(owner_id);
ALTER TABLE assessments ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE assessments ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_assessments_owner_id ON assessments(owner_id);
ALTER TABLE autosend_log ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE autosend_log ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_autosend_log_owner_id ON autosend_log(owner_id);
ALTER TABLE calendar_cache ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE calendar_cache ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_calendar_cache_owner_id ON calendar_cache(owner_id);
ALTER TABLE calendar_meetings ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE calendar_meetings ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_calendar_meetings_owner_id ON calendar_meetings(owner_id);
ALTER TABLE call_participants ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE call_participants ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_call_participants_owner_id ON call_participants(owner_id);
ALTER TABLE calls ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE calls ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_calls_owner_id ON calls(owner_id);
ALTER TABLE claims ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE claims ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_claims_owner_id ON claims(owner_id);
ALTER TABLE coach_reports ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE coach_reports ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_coach_reports_owner_id ON coach_reports(owner_id);
ALTER TABLE coach_state ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE coach_state ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_coach_state_owner_id ON coach_state(owner_id);
ALTER TABLE deal_health ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE deal_health ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_deal_health_owner_id ON deal_health(owner_id);
ALTER TABLE deal_health_history ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE deal_health_history ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_deal_health_history_owner_id ON deal_health_history(owner_id);
ALTER TABLE deal_people ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE deal_people ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_deal_people_owner_id ON deal_people(owner_id);
ALTER TABLE deal_risks ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE deal_risks ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_deal_risks_owner_id ON deal_risks(owner_id);
ALTER TABLE deal_stage_history ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE deal_stage_history ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_deal_stage_history_owner_id ON deal_stage_history(owner_id);
ALTER TABLE deals ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE deals ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_deals_owner_id ON deals(owner_id);
ALTER TABLE derived_outcomes ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE derived_outcomes ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_derived_outcomes_owner_id ON derived_outcomes(owner_id);
ALTER TABLE edges ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE edges ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_edges_owner_id ON edges(owner_id);
ALTER TABLE email_edits ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE email_edits ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_email_edits_owner_id ON email_edits(owner_id);
ALTER TABLE email_replies ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE email_replies ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_email_replies_owner_id ON email_replies(owner_id);
ALTER TABLE emails ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE emails ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_emails_owner_id ON emails(owner_id);
ALTER TABLE embeddings ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE embeddings ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_embeddings_owner_id ON embeddings(owner_id);
ALTER TABLE events ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE events ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_events_owner_id ON events(owner_id);
ALTER TABLE field_provenance ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE field_provenance ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_field_provenance_owner_id ON field_provenance(owner_id);
ALTER TABLE followup_decisions ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE followup_decisions ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_followup_decisions_owner_id ON followup_decisions(owner_id);
ALTER TABLE learned_patterns ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE learned_patterns ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_learned_patterns_owner_id ON learned_patterns(owner_id);
ALTER TABLE learning_proposals ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE learning_proposals ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_learning_proposals_owner_id ON learning_proposals(owner_id);
ALTER TABLE loops ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE loops ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_loops_owner_id ON loops(owner_id);
ALTER TABLE meddpicc ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE meddpicc ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_meddpicc_owner_id ON meddpicc(owner_id);
ALTER TABLE memory_conflicts ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE memory_conflicts ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_memory_conflicts_owner_id ON memory_conflicts(owner_id);
ALTER TABLE nodes ADD COLUMN owner_id TEXT DEFAULT 'local';
UPDATE nodes SET owner_id = NULL WHERE type IN ('account','person');
ALTER TABLE nodes ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
ALTER TABLE nodes ADD CONSTRAINT nodes_owner_id_check CHECK(owner_id IS NOT NULL OR type IN ('account','person'));
CREATE INDEX idx_nodes_owner_id ON nodes(owner_id);
ALTER TABLE nudges ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE nudges ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_nudges_owner_id ON nudges(owner_id);
ALTER TABLE pattern_observations ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE pattern_observations ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_pattern_observations_owner_id ON pattern_observations(owner_id);
ALTER TABLE prep_briefs ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE prep_briefs ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_prep_briefs_owner_id ON prep_briefs(owner_id);
ALTER TABLE reconciliations ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE reconciliations ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_reconciliations_owner_id ON reconciliations(owner_id);
ALTER TABLE reply_proposals ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE reply_proposals ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_reply_proposals_owner_id ON reply_proposals(owner_id);
ALTER TABLE seller_observations ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE seller_observations ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_seller_observations_owner_id ON seller_observations(owner_id);
ALTER TABLE seller_patterns ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE seller_patterns ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_seller_patterns_owner_id ON seller_patterns(owner_id);
ALTER TABLE slot_fills ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE slot_fills ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_slot_fills_owner_id ON slot_fills(owner_id);
ALTER TABLE sources ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE sources ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_sources_owner_id ON sources(owner_id);
ALTER TABLE speakers ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE speakers ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_speakers_owner_id ON speakers(owner_id);
ALTER TABLE stakeholders ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE stakeholders ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_stakeholders_owner_id ON stakeholders(owner_id);
ALTER TABLE turns ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE turns ALTER COLUMN owner_id SET DEFAULT NULLIF(current_setting('app.user_id', true), '');
CREATE INDEX idx_turns_owner_id ON turns(owner_id);

-- ---- the person row that IS a user -------------------------------------------------------------
ALTER TABLE people ADD COLUMN user_id TEXT UNIQUE;
UPDATE people SET user_id = 'local' WHERE node_id = (SELECT node_id FROM people WHERE is_me = 1 ORDER BY node_id LIMIT 1);

-- ---- keys re-scoped per owner ----------------------------------------------------------------
ALTER TABLE seller_patterns DROP CONSTRAINT seller_patterns_pkey;
ALTER TABLE seller_patterns ADD PRIMARY KEY (owner_id, tag);
ALTER TABLE calendar_cache DROP CONSTRAINT calendar_cache_pkey;
ALTER TABLE calendar_cache ADD PRIMARY KEY (owner_id, key);
ALTER TABLE calendar_meetings DROP CONSTRAINT calendar_meetings_pkey;
ALTER TABLE calendar_meetings ADD PRIMARY KEY (owner_id, event_id);
ALTER TABLE email_replies DROP CONSTRAINT email_replies_message_id_key;
ALTER TABLE email_replies ADD CONSTRAINT email_replies_owner_id_message_id_key UNIQUE (owner_id, message_id);
ALTER TABLE learned_patterns DROP CONSTRAINT learned_patterns_family_key_scope_key;
ALTER TABLE learned_patterns ADD CONSTRAINT learned_patterns_owner_id_family_key_scope_key UNIQUE (owner_id, family, key, scope);
-- lp:<family>:global:<key> -> lp:<family>:u:local:<key>, wherever a pattern id is stored
UPDATE learned_patterns SET id = regexp_replace(id, '^lp:([^:]+):global:', 'lp:\1:u:local:') WHERE id LIKE 'lp:%:global:%';
UPDATE learned_patterns SET merged_into = regexp_replace(merged_into, '^lp:([^:]+):global:', 'lp:\1:u:local:') WHERE merged_into LIKE 'lp:%:global:%';
UPDATE learning_proposals SET pattern_id = regexp_replace(pattern_id, '^lp:([^:]+):global:', 'lp:\1:u:local:') WHERE pattern_id LIKE 'lp:%:global:%';
UPDATE learning_proposals SET target_id = regexp_replace(target_id, '^lp:([^:]+):global:', 'lp:\1:u:local:') WHERE target_id LIKE 'lp:%:global:%';
UPDATE field_provenance SET entity_id = regexp_replace(entity_id, '^lp:([^:]+):global:', 'lp:\1:u:local:') WHERE entity_id LIKE 'lp:%:global:%';
UPDATE memory_conflicts SET entity_id = regexp_replace(entity_id, '^lp:([^:]+):global:', 'lp:\1:u:local:') WHERE entity_id LIKE 'lp:%:global:%';

-- ---- users -------------------------------------------------------------------------------------
CREATE TABLE users (
  id           TEXT PRIMARY KEY,
  email        TEXT UNIQUE,
  name         TEXT NOT NULL DEFAULT '',
  role         TEXT NOT NULL DEFAULT 'rep' CHECK(role IN ('rep','manager','admin')),
  team_id      TEXT,
  status       TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('invited','active','disabled')),
  google_sub   TEXT UNIQUE,
  extra_emails TEXT NOT NULL DEFAULT '[]',
  aliases      TEXT NOT NULL DEFAULT '[]',
  signature    TEXT,
  timezone     TEXT,
  languages    TEXT NOT NULL DEFAULT '[]',
  role_title   TEXT,
  style        TEXT,
  call_context TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT
);
CREATE TABLE teams (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE team_managers (
  team_id TEXT NOT NULL REFERENCES teams(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  PRIMARY KEY (team_id, user_id)
);
CREATE TABLE user_state (
  user_id    TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT,
  updated_at TEXT,
  PRIMARY KEY (user_id, key)
);
CREATE TABLE user_speaker_labels (
  user_id    TEXT NOT NULL,
  label_norm TEXT NOT NULL,
  label      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, label_norm)
);

-- ---- child rows take the parent's owner ------------------------------------------------------
-- Arguments: 'parent_table.parent_key=child_column' ...; the first argument whose child column is
-- not NULL decides. A NULL owner_id (no acting user) is filled from the parent; a different one is
-- refused with an integrity error, so a rep can never hang a row on another rep's call or deal.
CREATE OR REPLACE FUNCTION app_child_owner() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  parts text[];
  fk text;
  key_type text;
  parent_owner text;
  i int;
BEGIN
  FOR i IN 0 .. TG_NARGS - 1 LOOP
    parts := regexp_split_to_array(TG_ARGV[i], '[.=]');
    fk := to_jsonb(NEW) ->> parts[3];
    IF fk IS NOT NULL THEN
      SELECT format_type(a.atttypid, a.atttypmod) INTO key_type FROM pg_attribute a
        WHERE a.attrelid = parts[1]::regclass AND a.attname = parts[2];
      EXECUTE format('SELECT owner_id FROM %I WHERE %I = $1::%s', parts[1], parts[2], key_type)
        INTO parent_owner USING fk;
      IF parent_owner IS NOT NULL THEN
        IF NEW.owner_id IS NULL THEN
          NEW.owner_id := parent_owner;
        ELSIF NEW.owner_id <> parent_owner THEN
          RAISE EXCEPTION 'owner mismatch: % row for % owned by %, parent % % owned by %',
            TG_TABLE_NAME, TG_ARGV[i], NEW.owner_id, parts[1], fk, parent_owner
            USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
      END IF;
    END IF;
  END LOOP;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_agent_runs_owner BEFORE INSERT ON agent_runs FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
CREATE TRIGGER trg_artifacts_owner BEFORE INSERT ON artifacts FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
CREATE TRIGGER trg_assessments_owner BEFORE INSERT ON assessments FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
CREATE TRIGGER trg_autosend_log_owner BEFORE INSERT ON autosend_log FOR EACH ROW EXECUTE FUNCTION app_child_owner('emails.id=email_id');
CREATE TRIGGER trg_call_participants_owner BEFORE INSERT ON call_participants FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
CREATE TRIGGER trg_calls_owner BEFORE INSERT ON calls FOR EACH ROW EXECUTE FUNCTION app_child_owner('nodes.id=node_id');
CREATE TRIGGER trg_claims_owner BEFORE INSERT ON claims FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id', 'deals.node_id=deal_id');
CREATE TRIGGER trg_coach_state_owner BEFORE INSERT ON coach_state FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
CREATE TRIGGER trg_deal_health_owner BEFORE INSERT ON deal_health FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_deal_health_history_owner BEFORE INSERT ON deal_health_history FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_deal_people_owner BEFORE INSERT ON deal_people FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_deal_risks_owner BEFORE INSERT ON deal_risks FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_deal_stage_history_owner BEFORE INSERT ON deal_stage_history FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_deals_owner BEFORE INSERT ON deals FOR EACH ROW EXECUTE FUNCTION app_child_owner('nodes.id=node_id');
CREATE TRIGGER trg_derived_outcomes_owner BEFORE INSERT ON derived_outcomes FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_edges_owner BEFORE INSERT ON edges FOR EACH ROW EXECUTE FUNCTION app_child_owner('nodes.id=src');
CREATE TRIGGER trg_email_edits_owner BEFORE INSERT ON email_edits FOR EACH ROW EXECUTE FUNCTION app_child_owner('emails.id=email_id');
CREATE TRIGGER trg_email_replies_owner BEFORE INSERT ON email_replies FOR EACH ROW EXECUTE FUNCTION app_child_owner('emails.id=email_id');
CREATE TRIGGER trg_emails_owner BEFORE INSERT ON emails FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id', 'deals.node_id=deal_id');
CREATE TRIGGER trg_embeddings_owner BEFORE INSERT ON embeddings FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id', 'deals.node_id=deal_id');
CREATE TRIGGER trg_events_owner BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION app_child_owner('nodes.id=node_id');
CREATE TRIGGER trg_followup_decisions_owner BEFORE INSERT ON followup_decisions FOR EACH ROW EXECUTE FUNCTION app_child_owner('loops.node_id=loop_id', 'deals.node_id=deal_id');
CREATE TRIGGER trg_loops_owner BEFORE INSERT ON loops FOR EACH ROW EXECUTE FUNCTION app_child_owner('nodes.id=node_id', 'calls.node_id=call_id', 'deals.node_id=deal_id');
CREATE TRIGGER trg_meddpicc_owner BEFORE INSERT ON meddpicc FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_nudges_owner BEFORE INSERT ON nudges FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
CREATE TRIGGER trg_pattern_observations_owner BEFORE INSERT ON pattern_observations FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id', 'deals.node_id=deal_id');
CREATE TRIGGER trg_prep_briefs_owner BEFORE INSERT ON prep_briefs FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_reply_proposals_owner BEFORE INSERT ON reply_proposals FOR EACH ROW EXECUTE FUNCTION app_child_owner('email_replies.id=reply_id');
CREATE TRIGGER trg_seller_observations_owner BEFORE INSERT ON seller_observations FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
CREATE TRIGGER trg_slot_fills_owner BEFORE INSERT ON slot_fills FOR EACH ROW EXECUTE FUNCTION app_child_owner('emails.id=email_id');
CREATE TRIGGER trg_sources_owner BEFORE INSERT ON sources FOR EACH ROW EXECUTE FUNCTION app_child_owner('nodes.id=node_id');
CREATE TRIGGER trg_speakers_owner BEFORE INSERT ON speakers FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
CREATE TRIGGER trg_stakeholders_owner BEFORE INSERT ON stakeholders FOR EACH ROW EXECUTE FUNCTION app_child_owner('deals.node_id=deal_id');
CREATE TRIGGER trg_turns_owner BEFORE INSERT ON turns FOR EACH ROW EXECUTE FUNCTION app_child_owner('calls.node_id=call_id');
