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


def run(conn):
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
