-- Postgres migration 0004 (2026-09-25): Phase 3, Google sign-in. Hand-written; the SQLite side is
-- store/migrate.py migration 8 and the tail of store/schema-sales.sql, which tests/test_schema_parity.py
-- checks a database migrated through here against. The three tables here are SYSTEM (store/tenancy.py),
-- bookkeeping about a user rather than a rep's work; their row-level policies are in the repeatable
-- store/pg/rls.sql, applied after every numbered step (store/pgmigrate.py). sessions.id holds sha256 of
-- the session id, never the id (salescoach/sessions.py).

-- Which users row made an admin change (invite, role, team, disable). engine._emit leaves it NULL.
ALTER TABLE events ADD COLUMN actor_user_id TEXT;

-- A browser holds a signed random session id; everything else about the session is here, so an
-- admin can end it (disable the user, "log out everywhere") and it stops at the next request.
CREATE TABLE sessions (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id),
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT,
  ip           TEXT,
  user_agent   TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

-- An invite is an allow-list entry: the admin typed an address; the app sends nothing.
CREATE TABLE invites (
  email       TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id),
  invited_by  TEXT,
  created_at  TEXT NOT NULL,
  accepted_at TEXT
);

-- One live grant per (user, provider): AES-256-GCM ciphertext under SALESCOACH_TOKEN_KEYS.
CREATE TABLE oauth_tokens (
  user_id           TEXT NOT NULL REFERENCES users(id),
  provider          TEXT NOT NULL DEFAULT 'google',
  scopes            TEXT NOT NULL DEFAULT '[]',
  refresh_token_enc TEXT,
  access_token_enc  TEXT,
  key_id            TEXT NOT NULL,
  expires_at        TEXT,
  email             TEXT,
  status            TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','needs_reconsent','revoked')),
  last_error        TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT,
  PRIMARY KEY (user_id, provider)
);
