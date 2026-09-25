-- Postgres migration 0006 (2026-09-25, Phase 4: per-rep recorder connections). Hand-written; the SQLite
-- side is store/migrate.py migration 10 and the tracked store/schema-sales.sql, which
-- tests/test_schema_parity.py checks a database migrated through here against.
--
--   source_connections   OWNED: one rep's own recorder account (Fathom, Fireflies, tl;dv, Granola). The
--                        calls it delivers are that rep's. API key and webhook signing secret are
--                        AES-256-GCM ciphertext (execution/tokens.py key ring); the per-connection webhook
--                        token is kept as its sha256 only.
--
-- Row-level security (FORCED, the OWNED policies) and app_source_connection_owner(), which the push
-- webhook calls before it can bind the connection's owner, are in the repeatable store/pg/rls.sql.

CREATE TABLE IF NOT EXISTS source_connections (
  id                 TEXT PRIMARY KEY,
  owner_id           TEXT NOT NULL DEFAULT NULLIF(current_setting('app.user_id', true), ''),
  kind               TEXT NOT NULL,
  label              TEXT,
  secret_enc         TEXT,
  key_id             TEXT,
  status             TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','error','disconnected')),
  account_email      TEXT,
  state              TEXT NOT NULL DEFAULT '{}',
  last_poll_at       TEXT,
  last_ok_at         TEXT,
  next_poll_at       TEXT,
  failures           INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  webhook_token_hash TEXT,
  webhook_secret_enc TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT,
  UNIQUE(owner_id, kind)
);
CREATE INDEX idx_source_connections_owner_id ON source_connections(owner_id);
