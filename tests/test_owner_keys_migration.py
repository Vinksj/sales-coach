"""SQLite migration 12 (Phase 8): pattern_observations is rebuilt with UNIQUE(owner_id, family, key, subject)
and idx_lprop_open is made again on (owner_id, kind, subject), keeping every row."""
import sqlite3

import pytest

from salescoach.store import migrate, stores

pytestmark = pytest.mark.sqlite_only          # PRAGMA user_version and a table rebuild: SQLite itself

OLD_OBSERVATIONS = """CREATE TABLE pattern_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, family TEXT NOT NULL, key TEXT NOT NULL, subject TEXT NOT NULL,
  polarity TEXT, call_id TEXT, deal_id TEXT, email_id INTEGER, nudge_id INTEGER, evidence TEXT NOT NULL DEFAULT '{}',
  confidence TEXT, value REAL, outcome_kind TEXT, outcome_value TEXT, seller_id TEXT,
  source_is_replay INTEGER NOT NULL DEFAULT 0, excluded INTEGER NOT NULL DEFAULT 0, observed_at TEXT,
  created_at TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT 'local', UNIQUE(family, key, subject))"""


def _unique_sets(conn, table):
    out = []
    for _, name, unique, origin, _p in conn.execute(f"PRAGMA index_list({table})"):
        if unique and origin == "u":
            out.append(tuple(r[2] for r in conn.execute(f"PRAGMA index_info({name})")))
    return out


def test_migration_12_rescopes_both_keys_and_keeps_the_rows(tmp_path, monkeypatch):
    path = tmp_path / "sales.db"
    monkeypatch.setenv("SALES_DB", str(path))
    conn = stores.sales()
    conn.close()
    raw = sqlite3.connect(path)
    raw.executescript(f"""
        DROP TABLE pattern_observations;
        {OLD_OBSERVATIONS};
        INSERT INTO pattern_observations(family,key,subject,created_at,excluded) VALUES ('seller','a','call:1','t',1);
        INSERT INTO pattern_observations(family,key,subject,created_at) VALUES ('seller','b','call:1','t');
        DROP INDEX idx_lprop_open;
        CREATE UNIQUE INDEX idx_lprop_open ON learning_proposals(kind, subject) WHERE status='open';
        PRAGMA user_version = 11;
    """)
    raw.close()
    conn = stores.sales()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION == 13
        assert _unique_sets(conn, "pattern_observations") == [("owner_id", "family", "key", "subject")]
        rows = conn.execute("SELECT family, key, subject, excluded, owner_id FROM pattern_observations ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("seller", "a", "call:1", 1, "local"), ("seller", "b", "call:1", 0, "local")]
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_lprop_open'").fetchone()[0]
        assert "(owner_id, kind, subject)" in sql and "status='open'" in sql
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                             "AND tbl_name='pattern_observations'")}
        assert {"idx_pattern_observations_owner_id", "idx_pobs_family", "idx_pobs_call"} <= names
    finally:
        conn.close()
    migrate.run(stores.sales())                   # idempotent: a second run is a no-op


OLD_OUTCOMES = """CREATE TABLE derived_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
  deal_id TEXT, value INTEGER, computed_at TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
  owner_id TEXT NOT NULL DEFAULT 'local', UNIQUE(kind, subject_type, subject_id))"""


def test_migration_13_scopes_derived_outcomes_and_widens_access_log_keeping_the_rows(tmp_path, monkeypatch):
    path = tmp_path / "sales.db"
    monkeypatch.setenv("SALES_DB", str(path))
    stores.sales().close()
    raw = sqlite3.connect(path)
    raw.executescript(f"""
        DROP TABLE derived_outcomes;
        {OLD_OUTCOMES};
        INSERT INTO derived_outcomes(kind,subject_type,subject_id,value,computed_at,owner_id)
            VALUES ('email_replied','email','7',1,'t','u-m');
        DROP TABLE access_log;
        CREATE TABLE access_log (id INTEGER PRIMARY KEY AUTOINCREMENT, viewer_id TEXT NOT NULL,
            owner_user_id TEXT NOT NULL, entity_type TEXT NOT NULL CHECK(entity_type IN ('call','deal')),
            entity_id TEXT NOT NULL, viewed_at TEXT NOT NULL);
        INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) VALUES ('u-m','u-a','call','c1','t');
        PRAGMA user_version = 12;
    """)
    raw.close()
    conn = stores.sales()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION == 13
        assert _unique_sets(conn, "derived_outcomes") == [("owner_id", "kind", "subject_type", "subject_id")]
        # the owner's own row about the same subject no longer collides with another user's
        conn.execute("INSERT INTO derived_outcomes(kind,subject_type,subject_id,value,computed_at,owner_id) "
                     "VALUES ('email_replied','email','7',0,'t','u-a')")
        rows = conn.execute("SELECT owner_id, value FROM derived_outcomes ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("u-m", 1), ("u-a", 0)]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                             "AND tbl_name='derived_outcomes'")}
        assert {"idx_derived_outcomes_owner_id", "idx_outcomes_deal"} <= names
        # access_log takes the two new kinds (manager/views.KINDS) and keeps its rows
        conn.execute("INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) "
                     "VALUES ('u-m','u-a','coaching','u-a','t'), ('u-m','u-a','email','7','t')")
        assert [r[0] for r in conn.execute("SELECT entity_type FROM access_log ORDER BY id")] == ["call", "coaching", "email"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) "
                         "VALUES ('u-m','u-a','loop','x','t')")
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='idx_access_log_entity'").fetchone()
    finally:
        conn.close()
    migrate.run(stores.sales())                   # idempotent
