"""Migration 4 on a database created at version 3, with data in every table that names a call."""
import re
import sqlite3

import pytest

from salescoach.store import migrate, stores

V3_CALLS = """CREATE TABLE IF NOT EXISTS calls (
  node_id        TEXT PRIMARY KEY REFERENCES nodes(id),
  deal_id        TEXT REFERENCES nodes(id),
  source         TEXT NOT NULL CHECK(source IN ('capture','audio_file','granola','paste')),
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
  updated_at     TEXT
);"""
SOURCES = ("capture", "audio_file", "granola", "paste", "granola")


@pytest.fixture
def v3(tmp_path, monkeypatch):
    """A version-3 sales.db: today's schema with the calls table as it shipped before migration 4."""
    monkeypatch.setenv("SALESCOACH_NO_PLUGINS", "1")
    schema = stores.SCHEMA.read_text()
    current = re.search(r"CREATE TABLE IF NOT EXISTS calls \(.*?\n\);", schema, re.S).group(0)
    assert "history" in current and "CHECK(source" not in current
    old_schema = tmp_path / "schema-v3.sql"
    old_schema.write_text(schema.replace(current, V3_CALLS))
    path = tmp_path / "v3.db"
    conn = stores.engine.connect(str(path))
    stores.engine.init(conn, schema=str(old_schema))
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO nodes(id,type,title) VALUES ('deal-1','deal','Acme pilot')")
    conn.execute("INSERT INTO deals(node_id,name) VALUES ('deal-1','Acme pilot')")
    conn.execute("INSERT INTO nodes(id,type,title) VALUES ('person-1','person','Asha Rao')")
    conn.execute("INSERT INTO people(node_id,name,email) VALUES ('person-1','Asha Rao','asha@acme.test')")
    for i, source in enumerate(SOURCES):
        cid = f"call-{i}"
        conn.execute("INSERT INTO nodes(id,type,kind,title) VALUES (?,?,?,?)", (cid, "call", source, f"Call {i}"))
        conn.execute("INSERT INTO calls(node_id,deal_id,source,source_ref,title,started_at,ended_at,audio_dir,lang_mode,"
                     "transcript_sha,quality_score,wf_state,wf_error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, "deal-1" if i % 2 == 0 else None, source, f"{source}:{i}", f"Call {i}",
                      f"2026-09-0{i + 1}T10:00:00+00:00", None, "/audio/x" if source == "capture" else None,
                      "hinglish" if i == 0 else "auto", f"sha{i}", 0.5 + i / 10, "awaiting_review",
                      "quality_done: boom" if i == 3 else None, "2026-09-10T00:00:00+00:00"))
        conn.execute("INSERT INTO call_participants(call_id,person_id) VALUES (?, 'person-1')", (cid,))
        for idx in range(3):
            conn.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES (?,?,?,?,?)",
                         (cid, "final", idx, "me" if idx % 2 else "them", f"turn {idx}"))
        conn.execute("INSERT INTO nodes(id,type,title) VALUES (?,?,?)", (f"loop-{i}", "loop", "Send the deck"))
        conn.execute("INSERT INTO loops(node_id,deal_id,call_id,type,description,owner,source,confidence,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (f"loop-{i}", "deal-1", cid, "my_action", "Send the deck", "me",
                                                    "explicit_commitment", "explicit", "2026-09-10T00:00:00+00:00"))
        conn.execute("INSERT INTO emails(call_id,deal_id,subject,body,created_at) VALUES (?,?,?,?,?)",
                     (cid, "deal-1", "Follow-up", "Hi", "2026-09-10T00:00:00+00:00"))
    conn.execute("INSERT INTO nodes(id,type,title) VALUES ('call-null','call','no ref')")
    conn.execute("INSERT INTO calls(node_id,source,title) VALUES ('call-null','paste','no ref')")     # NULL source_ref
    conn.commit()
    before = [tuple(r) for r in conn.execute("SELECT * FROM calls ORDER BY node_id")]
    conn.close()
    return path, before


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("calls", "turns", "loops", "emails", "call_participants", "nodes")}


def test_migrates_a_v3_database_with_data(v3):
    path, before = v3
    conn = stores.sales(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION == 6
    assert _counts(conn) == {"calls": 6, "turns": 15, "loops": 5, "emails": 5, "call_participants": 5, "nodes": 13}
    # every old column of every row, unchanged and in the same order; history appended
    after = [tuple(r) for r in conn.execute("SELECT * FROM calls ORDER BY node_id")]
    assert [row[:-1] for row in after] == before
    assert {r["node_id"]: r["history"] for r in conn.execute("SELECT node_id, history FROM calls")} == {
        "call-0": 0, "call-1": 0, "call-2": 1, "call-3": 0, "call-4": 1, "call-null": 0}
    cols = [r[1] for r in conn.execute("PRAGMA table_info(calls)")]
    assert cols[-1] == "history" and cols[:3] == ["node_id", "deal_id", "source"]
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='calls'").fetchone()[0]
    assert "CHECK(source" not in sql and "calls_v4" not in sql
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='calls_v4'").fetchone()[0] == 0


def test_indexes_and_foreign_keys_are_intact(v3, monkeypatch):
    path, _ = v3
    monkeypatch.delenv("SALESCOACH_NO_PLUGINS")            # plugin tables too: none of them may point at calls
    conn = stores.sales(path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='learned_patterns'").fetchone()
    indexes = {r[1]: r[2] for r in conn.execute("PRAGMA index_list(calls)")}          # name -> unique
    assert indexes.get("idx_calls_deal") == 0 and indexes.get("idx_calls_state") == 0
    assert sum(indexes.values()) == 2                                                  # PRIMARY KEY + UNIQUE(source_ref)
    fks = {(r[2], r[3], r[4]) for r in conn.execute("PRAGMA foreign_key_list(calls)")}
    assert fks == {("nodes", "node_id", "id"), ("nodes", "deal_id", "id")}
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1                      # switched back on
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # nothing else in the schema ever pointed at calls, before or after
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        assert all(r[2] != "calls" for r in conn.execute(f"PRAGMA foreign_key_list({table})")), table
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0] == 0
    # the joins the app makes still find their rows
    assert conn.execute("SELECT COUNT(*) FROM loops l JOIN calls c ON c.node_id=l.call_id").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM turns t JOIN calls c ON c.node_id=t.call_id").fetchone()[0] == 15
    assert conn.execute("SELECT COUNT(*) FROM emails e JOIN calls c ON c.node_id=e.call_id").fetchone()[0] == 5
    plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM calls WHERE wf_state='live'"))
    assert "idx_calls_state" in plan


def test_constraints_that_stay_and_the_one_that_goes(v3):
    path, _ = v3
    conn = stores.sales(path)
    conn.execute("INSERT INTO nodes(id,type) VALUES ('call-new','call')")
    conn.execute("INSERT INTO calls(node_id,source,source_ref) VALUES ('call-new','fireflies','fireflies:abc')")
    assert conn.execute("SELECT history, wf_state, lang_mode FROM calls WHERE node_id='call-new'").fetchone()[:] == \
        (0, "live", "auto")
    conn.execute("INSERT INTO nodes(id,type) VALUES ('call-dup','call')")
    with pytest.raises(sqlite3.IntegrityError):                                        # UNIQUE(source_ref) survived
        conn.execute("INSERT INTO calls(node_id,source,source_ref) VALUES ('call-dup','fathom','fireflies:abc')")
    with pytest.raises(sqlite3.IntegrityError):                                        # CHECK(lang_mode) survived
        conn.execute("INSERT INTO calls(node_id,source,lang_mode) VALUES ('call-dup','fathom','klingon')")
    with pytest.raises(sqlite3.IntegrityError):                                        # FK to nodes survived
        conn.execute("INSERT INTO calls(node_id,source) VALUES ('no-such-node','fathom')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO calls(node_id,source,deal_id) VALUES ('call-dup','fathom','no-such-deal')")
    conn.execute("INSERT INTO calls(node_id,source) VALUES ('call-dup','fathom')")     # several NULL refs are fine


def test_running_again_is_a_no_op_and_a_fresh_database_matches(v3, tmp_path):
    path, _ = v3
    stores.sales(path).close()
    conn = stores.sales(path)
    migrate.run(conn)
    assert _counts(conn)["calls"] == 6
    fresh = stores.sales(tmp_path / "fresh.db")
    assert fresh.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION

    def shape(c):
        return [(r[1], r[2], r[3], r[4]) for r in c.execute("PRAGMA table_info(calls)")]
    assert shape(conn) == shape(fresh)
    assert {r[1] for r in conn.execute("PRAGMA index_list(calls)") if not r[1].startswith("sqlite_")} == \
           {r[1] for r in fresh.execute("PRAGMA index_list(calls)") if not r[1].startswith("sqlite_")}


def test_a_failure_half_way_leaves_the_v3_database_untouched(v3, monkeypatch):
    path, before = v3
    monkeypatch.setattr(migrate, "CALLS_V4", migrate.CALLS_V4.replace("history        INTEGER", "history INTEGER, source"))
    with pytest.raises(sqlite3.OperationalError):                                      # duplicate column name
        stores.sales(path)
    conn = stores.engine.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert [tuple(r) for r in conn.execute("SELECT * FROM calls ORDER BY node_id")] == before
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='calls_v4'").fetchone()[0] == 0
    monkeypatch.undo()
    monkeypatch.setenv("SALESCOACH_NO_PLUGINS", "1")
    assert stores.sales(path).execute("SELECT COUNT(*) FROM calls WHERE history=1").fetchone()[0] == 2


def test_a_copy_failure_rolls_back_everything(v3, monkeypatch):
    """The row-count guard: if the copy came up short, the old table must still be there."""
    path, before = v3
    monkeypatch.setattr(migrate, "_CALLS_V3_COLUMNS", ("node_id", "no_such_column"))
    conn = stores.engine.connect(str(path))
    have = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    assert "no_such_column" not in have
    # only node_id is copied -> NOT NULL source fails -> rollback
    with pytest.raises(sqlite3.IntegrityError):
        migrate.run(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3 and not conn.in_transaction
    assert [tuple(r) for r in conn.execute("SELECT * FROM calls ORDER BY node_id")] == before
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_several_handles_opening_at_once_migrate_once(v3):
    """serve opens the store from the web app, the worker and every scheduler thread at start-up."""
    import threading
    path, before = v3
    errors, barrier = [], threading.Barrier(4)

    def open_it():
        try:
            barrier.wait(timeout=10)
            conn = stores.sales(path)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION
            assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 6
            conn.close()
        except Exception as exc:                          # pragma: no cover - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=open_it) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    conn = stores.sales(path)
    assert [tuple(r)[:-1] for r in conn.execute("SELECT * FROM calls ORDER BY node_id")] == before
