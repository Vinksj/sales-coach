"""Schema migrations keyed on PRAGMA user_version.

A fresh database is created at the current version by schema-sales.sql, so a
migration only runs against a database created by an older build. Append new
steps; never edit a shipped one.

A step is either a SQL script or a function(conn). A function owns its own
transaction and sets user_version itself (migration 4 has to switch foreign
keys off, which SQLite only honours outside a transaction).
"""
import sqlite3

MIGRATIONS = {
    # 2026-09-12 review fixes: who owns a Jarvis link, and send-safety columns.
    2: """
        ALTER TABLE loops ADD COLUMN world_link TEXT;
        UPDATE loops SET world_link='imported' WHERE world_commitment_id IS NOT NULL AND source='world';
        UPDATE loops SET world_link='mirror' WHERE world_commitment_id IS NOT NULL AND world_link IS NULL
            AND review_state='confirmed';
        UPDATE loops SET world_link='adopted' WHERE world_commitment_id IS NOT NULL AND world_link IS NULL;
        ALTER TABLE emails ADD COLUMN rfc822_message_id TEXT;
        ALTER TABLE emails ADD COLUMN user_added_addrs TEXT NOT NULL DEFAULT '[]';
    """,
    # 2026-09-12 second review: a stuck 'sending' row remembers whether it tried to send or to save a draft.
    3: """
        ALTER TABLE emails ADD COLUMN attempt_mode TEXT;
    """,
}

# ---- 4 (2026-09-17, transcript sources): any recorder may be a call's source ---------------------
# calls.source carried CHECK(source IN ('capture','audio_file','granola','paste')). SQLite cannot drop a
# CHECK, so the table is rebuilt (sqlite.org/lang_altertable.html, the 12-step procedure). What was
# checked before writing this:
#   * nothing REFERENCES calls: every call_id / node_id column in the core schema and in the plugin
#     schemas points at nodes(id), so no other table's foreign key names this table;
#   * there are no triggers and no views anywhere in the schema;
#   * the only explicit indexes on calls are idx_calls_deal and idx_calls_state; the UNIQUE on
#     source_ref is an automatic index and comes back with the column definition.
# calls.history replaces the policy that the NAME 'granola' used to carry (no follow-up drafted, not
# claimed for Jarvis): existing granola rows are history, everything else is not.
CALLS_V4 = """
CREATE TABLE calls_v4 (
  node_id        TEXT PRIMARY KEY REFERENCES nodes(id),
  deal_id        TEXT REFERENCES nodes(id),
  source         TEXT NOT NULL,
  source_ref     TEXT UNIQUE,
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
  history        INTEGER NOT NULL DEFAULT 0
)
"""
_CALLS_V3_COLUMNS = ("node_id", "deal_id", "source", "source_ref", "title", "started_at", "ended_at", "audio_dir",
                     "lang_mode", "asr_live_model", "asr_final_model", "transcript_sha", "quality_score",
                     "wf_state", "wf_error", "updated_at")


def _rebuild_calls(conn):
    conn.commit()                                   # PRAGMA foreign_keys is a no-op inside a transaction
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        # The web server, the worker and every scheduler thread open the store at start-up; whoever
        # waited for the write lock must not rebuild a table that was just rebuilt.
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 4:
            conn.execute("ROLLBACK")
            return
        have = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
        cols = [c for c in _CALLS_V3_COLUMNS if c in have]
        before = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        conn.execute("DROP TABLE IF EXISTS calls_v4")
        conn.execute(CALLS_V4)
        history = "history" if "history" in have else "CASE WHEN source='granola' THEN 1 ELSE 0 END"
        conn.execute(f"INSERT INTO calls_v4({', '.join(cols)}, history) SELECT {', '.join(cols)}, {history} FROM calls")
        conn.execute("DROP TABLE calls")
        conn.execute("ALTER TABLE calls_v4 RENAME TO calls")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_deal  ON calls(deal_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_state ON calls(wf_state)")
        after = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        if after != before:
            raise sqlite3.IntegrityError(f"calls rebuild copied {after} of {before} rows")
        # Only what this step could have broken: the rebuilt table's own references (nothing references
        # calls). Review 3: checking the whole database here made an old orphan in ANY table (a row left
        # by a sqlite3-CLI fix, where foreign keys are off) refuse the migration on every open, for ever.
        broken = conn.execute("PRAGMA foreign_key_check(calls)").fetchall()
        if broken:
            raise sqlite3.IntegrityError(f"calls rebuild left {len(broken)} broken foreign keys: "
                                         f"{[tuple(r) for r in broken[:5]]}")
        conn.execute("PRAGMA user_version = 4")     # inside the transaction: all of it lands, or none
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


MIGRATIONS[4] = _rebuild_calls


# ---- 5 (2026-09-19, neutral stored words): the execution plugin's tables named a person ----------
# followup_decisions.stage and .decision each had a CHECK whose last value was spelt with the original
# owner's first name ("the user asked for this" / "ask the user"), and email_replies carried a
# needs_<that name> column. They are now 'user', 'ask_user' and needs_user. The tracked source no
# longer contains the old spelling, so this step names nothing: a stage or decision outside today's
# vocabulary can only be the one value the old CHECK allowed, and the one needs_* column that is not
# needs_user is the old column. SQLite cannot drop a CHECK, so followup_decisions is rebuilt (the same
# 12-step procedure as migration 4, values translated in the copy); the column is renamed in place
# (ALTER TABLE ... RENAME COLUMN, SQLite 3.25+), or by the same rebuild on an older library.
#   * Both are plugin tables: plugins/execution.sql creates them (IF NOT EXISTS) on every connect,
#     AFTER this runs. A database that never had them (SALESCOACH_NO_PLUGINS) is skipped here and
#     gets today's shape from the plugin; one that has them keeps the rebuilt/renamed table, which
#     the plugin's IF NOT EXISTS leaves alone.
#   * Neither table declares a foreign key, nothing references either, there are no triggers or
#     views, so the rebuild needs no PRAGMA foreign_keys dance.
FOLLOWUP_DECISIONS_V5 = """
CREATE TABLE followup_decisions_v5 (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  loop_id           TEXT NOT NULL,
  deal_id           TEXT,
  eval_date         TEXT NOT NULL,
  stage             TEXT NOT NULL CHECK(stage IN ('rules','agent','user')),
  check_name        TEXT,
  decision          TEXT NOT NULL CHECK(decision IN
                      ('send_nudge','wait_until','escalate','close_as_stale','ask_user','skip')),
  rationale         TEXT NOT NULL,
  relationship_risk TEXT,
  relationship_note TEXT,
  wait_until        TEXT,
  next_check_at     TEXT,
  facts             TEXT NOT NULL DEFAULT '{}',
  run_id            INTEGER,
  nudge_run_id      INTEGER,
  email_id          INTEGER,
  risk_loop_id      TEXT,
  conflict_id       INTEGER,
  sent_counted_at   TEXT,
  created_at        TEXT NOT NULL
)
"""
FOLLOWUP_DECISIONS_V5_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_fud_loop  ON followup_decisions(loop_id, eval_date)",
    "CREATE INDEX IF NOT EXISTS idx_fud_email ON followup_decisions(email_id)",
)
FOLLOWUP_DECISIONS_V5_VALUES = {
    "stage": "CASE WHEN stage IN ('rules','agent') THEN stage ELSE 'user' END",
    "decision": "CASE WHEN decision IN ('send_nudge','wait_until','escalate','close_as_stale','skip') "
                "THEN decision ELSE 'ask_user' END",
}
EMAIL_REPLIES_V5 = """
CREATE TABLE email_replies_v5 (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id   TEXT NOT NULL UNIQUE,
  thread_id    TEXT NOT NULL,
  email_id     INTEGER,
  deal_id      TEXT,
  person_id    TEXT,
  from_addr    TEXT NOT NULL,
  from_name    TEXT,
  subject      TEXT,
  received_at  TEXT NOT NULL,
  body         TEXT NOT NULL,
  body_full    TEXT,
  status       TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','analyzed','failed','reviewed')),
  summary      TEXT,
  needs_user   INTEGER NOT NULL DEFAULT 0,
  ignored_instructions TEXT NOT NULL DEFAULT '[]',
  run_id       INTEGER,
  error        TEXT,
  created_at   TEXT NOT NULL,
  reviewed_at  TEXT
)
"""
EMAIL_REPLIES_V5_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_replies_deal   ON email_replies(deal_id, received_at)",
    "CREATE INDEX IF NOT EXISTS idx_replies_thread ON email_replies(thread_id)",
)
RENAME_COLUMN_SINCE = (3, 25, 0)


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _rebuild(conn, table, ddl, indexes, values):
    """Recreate `table` from `ddl` (which creates `<table>_v5`), copying every column the two shapes
    share; `values` maps a new column to the SQL expression that fills it instead."""
    new = f"{table}_v5"
    before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.execute(f"DROP TABLE IF EXISTS {new}")
    conn.execute(ddl)
    old_cols = set(_columns(conn, table))
    targets = [c for c in _columns(conn, new) if c in values or c in old_cols]
    sources = [values.get(c, c) for c in targets]
    conn.execute(f"INSERT INTO {new}({', '.join(targets)}) SELECT {', '.join(sources)} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {new} RENAME TO {table}")
    for index in indexes:
        conn.execute(index)
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if after != before:
        raise sqlite3.IntegrityError(f"{table} rebuild copied {after} of {before} rows")


def _neutral_words(conn):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 5:      # another handle got here first
            conn.execute("ROLLBACK")
            return
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "followup_decisions" in tables:
            _rebuild(conn, "followup_decisions", FOLLOWUP_DECISIONS_V5, FOLLOWUP_DECISIONS_V5_INDEXES,
                     FOLLOWUP_DECISIONS_V5_VALUES)
        if "email_replies" in tables:
            cols = _columns(conn, "email_replies")
            legacy = [c for c in cols if c.startswith("needs_") and c != "needs_user"]
            if len(legacy) > 1:
                raise sqlite3.IntegrityError(f"email_replies: expected one needs_* column to rename, found {legacy}")
            if "needs_user" not in cols and not legacy:
                # A table created by an early plugin SQL, before the column existed: CREATE IF NOT EXISTS
                # never adds columns, so add it (and its sibling) here rather than refuse to start.
                conn.execute("ALTER TABLE email_replies ADD COLUMN needs_user INTEGER NOT NULL DEFAULT 0")
                if "ignored_instructions" not in cols:
                    conn.execute("ALTER TABLE email_replies ADD COLUMN ignored_instructions TEXT NOT NULL DEFAULT '[]'")
            elif "needs_user" not in cols:
                if sqlite3.sqlite_version_info >= RENAME_COLUMN_SINCE:
                    conn.execute(f"ALTER TABLE email_replies RENAME COLUMN {legacy[0]} TO needs_user")
                else:
                    _rebuild(conn, "email_replies", EMAIL_REPLIES_V5, EMAIL_REPLIES_V5_INDEXES,
                             {"needs_user": legacy[0]})
        conn.execute("PRAGMA user_version = 5")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


MIGRATIONS[5] = _neutral_words

# ---- 6 (2026-09-24, two backends): the bus no longer computes "not before" from updated_at with
# julianday(), which Postgres does not have. fail() and defer() write the earliest retry time into
# not_before; NULL means claimable now, so every existing pending row stays claimable.
def _not_before(conn):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 6:      # another handle got here first
            conn.execute("ROLLBACK")
            return
        # A database whose tables were created by a newer schema-sales.sql and then rewound (the test
        # fixtures do this) already has the column: adding it twice is the only way this step can fail.
        # One without the table at all (a hand-built fixture) has nothing to migrate.
        cols = _columns(conn, "wf_events")
        if cols and "not_before" not in cols:
            conn.execute("ALTER TABLE wf_events ADD COLUMN not_before TEXT")
        conn.execute("PRAGMA user_version = 6")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


MIGRATIONS[6] = _not_before

# ---- 7 (2026-09-24, identity and ownership): every OWNED table (store/tenancy.py) gains owner_id,
# 'local' for everything this single-user file holds; people gains user_id (the person row that IS a
# user; the one is_me row becomes the local user's); the keys that would collide between two users
# are re-scoped on (owner_id, ...) by rebuilding the table (SQLite cannot change a PRIMARY KEY or a
# UNIQUE in place): seller_patterns, calendar_cache, calendar_meetings, email_replies,
# learned_patterns (whose ids go from lp:<family>:global:<key> to lp:<family>:u:local:<key>, in
# merged_into, learning_proposals and the memory gate's rows too); the users, teams, team_managers,
# user_state and user_speaker_labels tables appear. The DDL below is frozen at version 7 on purpose:
# a later version's columns are added by that version's step, never by this one.
#   * Nothing references any rebuilt table by foreign key (every FK in the schema points at nodes,
#     emails or agent_runs), and there are no triggers or views, so no PRAGMA foreign_keys dance.
#   * Plugin tables that are not there (SALESCOACH_NO_PLUGINS) are created later by the plugin SQL in
#     today's shape; the ones that are there are migrated here, before the plugin's IF NOT EXISTS runs.
OWNED_V7 = (
    "edges", "events", "sources", "deals", "deal_people", "calls", "call_participants", "turns", "speakers",
    "agent_runs", "artifacts", "claims", "assessments", "reconciliations", "loops", "emails", "email_edits",
    "seller_observations", "field_provenance", "memory_conflicts",
    "followup_decisions", "reply_proposals", "slot_fills", "autosend_log",
    "stakeholders", "meddpicc", "deal_risks", "deal_health", "deal_health_history", "coach_reports", "prep_briefs",
    "embeddings", "deal_stage_history", "derived_outcomes", "pattern_observations", "learning_proposals",
    "nudges", "coach_state",
)
REKEYED_V7 = ("seller_patterns", "calendar_cache", "calendar_meetings", "email_replies", "learned_patterns")
USER_TABLES_V7 = """
CREATE TABLE IF NOT EXISTS users (
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
CREATE TABLE IF NOT EXISTS user_state (
  user_id    TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT,
  updated_at TEXT,
  PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS user_speaker_labels (
  user_id    TEXT NOT NULL,
  label_norm TEXT NOT NULL,
  label      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, label_norm)
);
"""
PEOPLE_V7 = """CREATE TABLE people_v7 (
  node_id          TEXT PRIMARY KEY REFERENCES nodes(id),
  name             TEXT NOT NULL,
  email            TEXT UNIQUE,
  account_id       TEXT REFERENCES nodes(id),
  title            TEXT,
  is_me            INTEGER NOT NULL DEFAULT 0,
  contact_file     TEXT,
  world_contact_id TEXT,
  user_id          TEXT UNIQUE
)"""
SELLER_PATTERNS_V7 = """CREATE TABLE seller_patterns_v7 (
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
)"""
CALENDAR_CACHE_V7 = """CREATE TABLE calendar_cache_v7 (
  key        TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  source     TEXT,
  events     TEXT NOT NULL DEFAULT '[]',
  error      TEXT,
  owner_id   TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (owner_id, key)
)"""
CALENDAR_MEETINGS_V7 = """CREATE TABLE calendar_meetings_v7 (
  event_id        TEXT NOT NULL,
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
  record          TEXT NOT NULL DEFAULT 'no',
  call_id         TEXT,
  meeting_url     TEXT,
  last_seen_at    TEXT,
  record_error    TEXT,
  owner_id        TEXT NOT NULL DEFAULT 'local',
  PRIMARY KEY (owner_id, event_id)
)"""
EMAIL_REPLIES_V7 = """CREATE TABLE email_replies_v7 (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id   TEXT NOT NULL,
  thread_id    TEXT NOT NULL,
  email_id     INTEGER,
  deal_id      TEXT,
  person_id    TEXT,
  from_addr    TEXT NOT NULL,
  from_name    TEXT,
  subject      TEXT,
  received_at  TEXT NOT NULL,
  body         TEXT NOT NULL,
  body_full    TEXT,
  status       TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','analyzed','failed','reviewed')),
  summary      TEXT,
  needs_user   INTEGER NOT NULL DEFAULT 0,
  ignored_instructions TEXT NOT NULL DEFAULT '[]',
  run_id       INTEGER,
  error        TEXT,
  created_at   TEXT NOT NULL,
  reviewed_at  TEXT,
  owner_id     TEXT NOT NULL DEFAULT 'local',
  UNIQUE(owner_id, message_id)
)"""
LEARNED_PATTERNS_V7 = """CREATE TABLE learned_patterns_v7 (
  id          TEXT PRIMARY KEY,
  family      TEXT NOT NULL,
  key         TEXT NOT NULL,
  scope       TEXT NOT NULL DEFAULT 'global',
  polarity    TEXT,
  n_obs       INTEGER NOT NULL DEFAULT 0,
  n_calls     INTEGER NOT NULL DEFAULT 0,
  n_deals     INTEGER NOT NULL DEFAULT 0,
  support     INTEGER NOT NULL DEFAULT 0,
  label       TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','active','dormant','retired')),
  user_state  TEXT CHECK(user_state IS NULL OR user_state IN ('confirmed','wrong','retired')),
  merged_into TEXT,
  first_seen  TEXT,
  last_seen   TEXT,
  returned    INTEGER NOT NULL DEFAULT 0,
  summary     TEXT,
  stats       TEXT NOT NULL DEFAULT '{}',
  seller_id   TEXT,
  updated_at  TEXT,
  no_prompt   INTEGER NOT NULL DEFAULT 0,
  owner_id    TEXT NOT NULL DEFAULT 'local',
  UNIQUE(owner_id, family, key, scope)
)"""
REBUILT_V7 = {
    "people": (PEOPLE_V7, ()),
    "seller_patterns": (SELLER_PATTERNS_V7, ()),
    "calendar_cache": (CALENDAR_CACHE_V7, ()),
    "calendar_meetings": (CALENDAR_MEETINGS_V7, ("CREATE INDEX IF NOT EXISTS idx_calmeet_start ON calendar_meetings(start_at)",)),
    "email_replies": (EMAIL_REPLIES_V7, ("CREATE INDEX IF NOT EXISTS idx_replies_deal   ON email_replies(deal_id, received_at)",
                                        "CREATE INDEX IF NOT EXISTS idx_replies_thread ON email_replies(thread_id)")),
    "learned_patterns": (LEARNED_PATTERNS_V7, ("CREATE INDEX IF NOT EXISTS idx_lp_family ON learned_patterns(family, status)",)),
}


def _rebuild_v7(conn, table, ddl, indexes):
    """Like _rebuild, but the new table is <table>_v7, a column missing on either side is skipped, and a
    NULL in a column that is NOT NULL today (a row an early plugin wrote before the constraint) is copied
    as '' / 0 rather than refusing to open the store."""
    new = f"{table}_v7"
    before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.execute(f"DROP TABLE IF EXISTS {new}")
    conn.execute(ddl)
    old_cols = set(_columns(conn, table))
    targets, sources = [], []
    for _, name, ctype, notnull, default, pk in conn.execute(f"PRAGMA table_info({new})"):
        if name not in old_cols:
            continue
        targets.append(name)
        if notnull and default is None and not pk:
            filler = "0" if (ctype or "").upper() in ("INTEGER", "REAL") else "''"
            sources.append(f"COALESCE({name}, {filler})")
        else:
            sources.append(name)
    conn.execute(f"INSERT INTO {new}({', '.join(targets)}) SELECT {', '.join(sources)} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {new} RENAME TO {table}")
    for index in indexes:
        conn.execute(index)
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if after != before:
        raise sqlite3.IntegrityError(f"{table} rebuild copied {after} of {before} rows")


def _rewrite_pattern_ids(conn, tables):
    """lp:<family>:global:<key> -> lp:<family>:u:local:<key>, wherever a pattern id is stored."""
    def fix(table, column):
        if table not in tables or column not in _columns(conn, table):
            return
        conn.execute(f"UPDATE {table} SET {column} = 'lp:' || substr({column}, 4, instr(substr({column}, 4), ':') - 1) "
                     f"|| ':u:local:' || substr({column}, 4 + instr(substr({column}, 4), ':') + length('global:')) "
                     f"WHERE {column} LIKE 'lp:%:global:%'")
    fix("learned_patterns", "id")
    fix("learned_patterns", "merged_into")
    fix("learning_proposals", "pattern_id")
    fix("learning_proposals", "target_id")
    fix("field_provenance", "entity_id")
    fix("memory_conflicts", "entity_id")


def _identity(conn):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 7:      # another handle got here first
            conn.execute("ROLLBACK")
            return
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "nodes" in tables and "owner_id" not in _columns(conn, "nodes"):
            conn.execute("ALTER TABLE nodes ADD COLUMN owner_id TEXT DEFAULT 'local'")
            conn.execute("UPDATE nodes SET owner_id=NULL WHERE type IN ('account','person')")
        for table in OWNED_V7:
            if table in tables and "owner_id" not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local'")
        for table, (ddl, indexes) in REBUILT_V7.items():
            if table in tables and (table == "people" and "user_id" not in _columns(conn, table)
                                    or table != "people" and "owner_id" not in _columns(conn, table)):
                _rebuild_v7(conn, table, ddl, indexes)
        if "people" in tables:
            me = conn.execute("SELECT node_id FROM people WHERE is_me=1 AND user_id IS NULL "
                              "AND NOT EXISTS (SELECT 1 FROM people WHERE user_id='local') "
                              "ORDER BY node_id LIMIT 1").fetchone()
            if me:
                conn.execute("UPDATE people SET user_id='local' WHERE node_id=?", (me[0],))
        _rewrite_pattern_ids(conn, tables)
        for table in ("nodes", *OWNED_V7, *REKEYED_V7):
            if table in tables:
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_owner_id ON {table}(owner_id)")
        for statement in USER_TABLES_V7.split(";"):        # one by one: executescript would commit first
            if statement.strip():
                conn.execute(statement)
        conn.execute("PRAGMA user_version = 7")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


MIGRATIONS[7] = _identity


# ---- 8 (2026-09-25, Phase 3: Google sign-in): sessions, invites, encrypted OAuth grants, audit actor ----
# Mirrors store/pg/0004_auth.sql. The three tables are the tail of schema-sales.sql verbatim; the
# events column records which users row made an admin change (engine._emit leaves it NULL).
AUTH_TABLES_V8 = """
CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id),
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT,
  ip           TEXT,
  user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS invites (
  email       TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id),
  invited_by  TEXT,
  created_at  TEXT NOT NULL,
  accepted_at TEXT
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
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
"""


def _auth(conn):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 8:      # another handle got here first
            conn.execute("ROLLBACK")
            return
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "events" in tables and "actor_user_id" not in _columns(conn, "events"):
            conn.execute("ALTER TABLE events ADD COLUMN actor_user_id TEXT")
        for statement in AUTH_TABLES_V8.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute("PRAGMA user_version = 8")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


MIGRATIONS[8] = _auth


# ---- 9 (2026-09-25, Phase 6: processes, fairness, budgets, org settings, raw payloads) ------------------
# wf_events gains priority (claimed highest first) and owner_id (one running event per owner; the fairness
# rule in orchestrator/bus.claim_next; `owner`, not owner_id: the bus is SYSTEM, the column is a routing key); org_settings holds the settings overlay in cloud mode (config.py);
# raw_payloads holds what a source delivered (sources/base.save_raw in cloud mode). Nothing is rebuilt.
# The Postgres side is store/pg/0005_ops.sql.
OPS_TABLES_V9 = """
CREATE TABLE IF NOT EXISTS org_settings (
  name       TEXT PRIMARY KEY,
  body       TEXT NOT NULL DEFAULT '{}',
  version    INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT,
  updated_by TEXT
);
CREATE TABLE IF NOT EXISTS raw_payloads (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id    TEXT NOT NULL DEFAULT 'local',
  source_kind TEXT NOT NULL,
  source_ref  TEXT,
  encoding    TEXT NOT NULL DEFAULT 'text' CHECK(encoding IN ('text','json','base64')),
  body        TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  UNIQUE(owner_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_raw_payloads_owner_id ON raw_payloads(owner_id);
CREATE INDEX IF NOT EXISTS idx_wf_claim ON wf_events(status, priority, id);
"""


def _ops(conn):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 9:      # another handle got here first
            conn.execute("ROLLBACK")
            return
        cols = _columns(conn, "wf_events")
        if cols and "priority" not in cols:
            conn.execute("ALTER TABLE wf_events ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        if cols and "owner" not in cols:
            conn.execute("ALTER TABLE wf_events ADD COLUMN owner TEXT")
        for statement in OPS_TABLES_V9.split(";"):        # one by one: executescript would commit first
            if statement.strip() and (cols or "idx_wf_claim" not in statement):   # a fixture without the bus table
                conn.execute(statement)
        conn.execute("PRAGMA user_version = 9")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


MIGRATIONS[9] = _ops


def run(conn):
    """Apply every step above the database's version, in order; the version ends at the highest step
    applied."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target in sorted(MIGRATIONS):
        if target > version:
            step = MIGRATIONS[target]
            if callable(step):
                step(conn)
                continue
            conn.executescript(step)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
