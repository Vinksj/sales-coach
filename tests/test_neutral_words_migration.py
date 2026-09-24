"""Migration 5 on a database created at version 4, whose execution-plugin tables still carry the
spellings an older build stored: a stage value and a decision value outside today's vocabulary,
and a needs_<old word> column on email_replies.

The migration names no old spelling (the source no longer contains it), so the fixture stands in
any out-of-vocabulary word: 'boss' here, whatever the older build wrote on a real install.
"""
import re
import sqlite3
import threading

import pytest

from salescoach.store import migrate, stores

OLD = "boss"
NEW_DDL = stores.PLUGINS_DIR.joinpath("execution.sql").read_text()
OLD_DDL = (NEW_DDL.replace("'user'", f"'{OLD}'").replace("'ask_user'", f"'ask_{OLD}'")
           .replace("needs_user", f"needs_{OLD}"))
assert OLD_DDL != NEW_DDL and "needs_user" not in OLD_DDL and "'user'" not in OLD_DDL

DECISIONS = [  # (stage, check_name, decision)
    ("rules", "completed", f"ask_{OLD}"),
    ("agent", "agent", "send_nudge"),
    (OLD, "requested", "send_nudge"),
    (OLD, "own_commitment", f"ask_{OLD}"),
    ("rules", "cadence", "wait_until"),
    ("agent", "agent", "close_as_stale"),
    ("rules", "skip", "skip"),
    ("agent", "agent", "escalate"),
]
REPLIES = [1, 0, 1]     # needs_<OLD>


def _v4_db(tmp_path, with_plugin_tables=True):
    path = tmp_path / "v4.db"
    conn = stores.engine.connect(str(path))
    stores.engine.init(conn, schema=str(stores.SCHEMA))
    conn.execute("PRAGMA user_version = 4")
    if with_plugin_tables:
        conn.executescript(OLD_DDL)
        for i, (stage, check, decision) in enumerate(DECISIONS):
            conn.execute("INSERT INTO followup_decisions(loop_id,deal_id,eval_date,stage,check_name,decision,rationale,"
                         "relationship_risk,wait_until,facts,run_id,email_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"loop-{i % 3}", "deal-1" if i % 2 else None, f"2026-09-{10 + i:02d}", stage, check, decision,
                          f"because {i}", "high" if i == 3 else None, "2026-10-01" if decision == "wait_until" else None,
                          '{"n": %d}' % i, i if i % 2 else None, 100 + i if decision == "send_nudge" else None,
                          "2026-09-10T00:00:00+00:00"))
        for i, needs in enumerate(REPLIES):
            conn.execute(f"INSERT INTO email_replies(message_id,thread_id,email_id,deal_id,from_addr,from_name,subject,"
                         f"received_at,body,status,summary,needs_{OLD},ignored_instructions,created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"<m{i}@x>", f"t{i}", 100 + i, "deal-1", "asha@acme.test", "Asha", f"Re: {i}",
                          f"2026-09-1{i}T09:00:00+00:00", f"body {i}", "analyzed" if needs else "new",
                          f"summary {i}" if needs else None, needs, "[]", "2026-09-10T00:00:00+00:00"))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def v4(tmp_path, monkeypatch):
    """A version-4 sales.db with old-shaped execution tables and rows carrying the old words; the
    plugins are ON, so the open that migrates it also re-applies today's execution.sql afterwards."""
    monkeypatch.delenv("SALESCOACH_NO_PLUGINS", raising=False)
    path = _v4_db(tmp_path)
    conn = stores.engine.connect(str(path))
    before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]
              for t in ("followup_decisions", "email_replies")}
    conn.close()
    return path, before


def _cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _shape(conn, table):
    return [(r[1], r[2], r[3], r[4], r[5]) for r in conn.execute(f"PRAGMA table_info({table})")]


def _indexes(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})") if not r[1].startswith("sqlite_")}


def test_migrates_a_v4_database_with_the_old_words(v4):
    path, before = v4
    conn = stores.sales(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION == 6

    # followup_decisions: every row kept, only the two words translated, everything else byte-equal
    rows = [tuple(r) for r in conn.execute("SELECT * FROM followup_decisions ORDER BY id")]
    assert len(rows) == len(before["followup_decisions"]) == len(DECISIONS)
    cols = _cols(conn, "followup_decisions")
    stage_i, decision_i = cols.index("stage"), cols.index("decision")
    for old, new in zip(before["followup_decisions"], rows):
        expected = list(old)
        expected[stage_i] = "user" if old[stage_i] == OLD else old[stage_i]
        expected[decision_i] = "ask_user" if old[decision_i] == f"ask_{OLD}" else old[decision_i]
        assert new == tuple(expected)
    assert [r[0] for r in conn.execute("SELECT stage FROM followup_decisions ORDER BY id")] == \
        ["user" if s == OLD else s for s, _, _ in DECISIONS]
    assert conn.execute("SELECT COUNT(*) FROM followup_decisions WHERE stage='user'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM followup_decisions WHERE decision='ask_user'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM followup_decisions WHERE stage=? OR decision=?",
                        (OLD, f"ask_{OLD}")).fetchone()[0] == 0

    # email_replies: the column is renamed in place, values and every other column untouched
    cols = _cols(conn, "email_replies")
    assert "needs_user" in cols and f"needs_{OLD}" not in cols
    assert cols.index("needs_user") == cols.index("summary") + 1                         # same position
    rows = [tuple(r) for r in conn.execute("SELECT * FROM email_replies ORDER BY id")]
    assert rows == before["email_replies"]
    assert [r[0] for r in conn.execute("SELECT needs_user FROM email_replies ORDER BY id")] == REPLIES

    # the schema now is today's: no old word in any CHECK, no scratch table left behind
    assert "_v5" not in " ".join(r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    ddl = " ".join(r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name IN ('followup_decisions','email_replies') AND sql IS NOT NULL"))
    assert OLD not in ddl
    assert "CHECK(stage IN ('rules','agent','user'))" in ddl
    assert re.search(r"needs_user\s+INTEGER NOT NULL DEFAULT 0", ddl)


def test_the_new_constraints_hold_and_the_old_values_are_refused(v4):
    path, _ = v4
    conn = stores.sales(path)
    ok = ("INSERT INTO followup_decisions(loop_id,eval_date,stage,decision,rationale,created_at) VALUES ('l','d',?,?,'r','t')")
    conn.execute(ok, ("user", "ask_user"))
    conn.execute(ok, ("rules", "skip"))
    for stage, decision in ((OLD, "skip"), ("rules", f"ask_{OLD}"), ("nobody", "skip")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ok, (stage, decision))
    conn.execute("INSERT INTO email_replies(message_id,thread_id,from_addr,received_at,body,created_at) "
                 "VALUES ('<new@x>','t','a@b','2026-09-19','hi','t')")
    assert conn.execute("SELECT needs_user FROM email_replies WHERE message_id='<new@x>'").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):                                         # UNIQUE(message_id) survived
        conn.execute("INSERT INTO email_replies(message_id,thread_id,from_addr,received_at,body,created_at) "
                     "VALUES ('<new@x>','t','a@b','2026-09-19','hi','t')")
    with pytest.raises(sqlite3.OperationalError):                                       # the old column is gone
        conn.execute(f"SELECT needs_{OLD} FROM email_replies")
    assert _indexes(conn, "followup_decisions") == {"idx_fud_loop", "idx_fud_email"}
    assert _indexes(conn, "email_replies") == {"idx_replies_deal", "idx_replies_thread"}
    plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM followup_decisions WHERE loop_id='l'"))
    assert "idx_fud_loop" in plan
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0] == 0


def test_running_again_is_a_no_op_and_a_fresh_database_has_the_same_shape(v4, tmp_path):
    path, _ = v4
    stores.sales(path).close()
    conn = stores.sales(path)
    migrate.run(conn)
    migrate.MIGRATIONS[5](conn)                                                          # the step itself, again
    assert conn.execute("SELECT COUNT(*) FROM followup_decisions").fetchone()[0] == len(DECISIONS)
    assert conn.execute("SELECT COUNT(*) FROM email_replies").fetchone()[0] == len(REPLIES)
    assert not conn.in_transaction
    fresh = stores.sales(tmp_path / "fresh.db")
    assert fresh.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION
    for table in ("followup_decisions", "email_replies"):
        assert _shape(conn, table) == _shape(fresh, table), table
        assert _indexes(conn, table) == _indexes(fresh, table), table
    ddl = fresh.execute("SELECT sql FROM sqlite_master WHERE name='email_replies'").fetchone()[0]
    assert "needs_user" in ddl and OLD not in ddl


def test_a_v4_database_without_the_plugin_tables_gets_them_in_todays_shape(tmp_path, monkeypatch):
    """SALESCOACH_NO_PLUGINS installs never had the tables: the step skips them and the plugin's
    CREATE IF NOT EXISTS, which runs after every migration, creates today's shape."""
    path = _v4_db(tmp_path, with_plugin_tables=False)
    monkeypatch.setenv("SALESCOACH_NO_PLUGINS", "1")
    conn = stores.sales(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='followup_decisions'").fetchone()
    conn.close()
    monkeypatch.delenv("SALESCOACH_NO_PLUGINS")
    conn = stores.sales(path)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='followup_decisions'").fetchone()[0]
    assert "'user'" in ddl and "'ask_user'" in ddl
    assert "needs_user" in _cols(conn, "email_replies")


def test_the_rebuild_fallback_for_an_old_sqlite_gives_the_same_result(v4, monkeypatch):
    path, before = v4
    monkeypatch.setattr(migrate, "RENAME_COLUMN_SINCE", (99, 0, 0))
    conn = stores.sales(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION
    assert [tuple(r) for r in conn.execute("SELECT * FROM email_replies ORDER BY id")] == before["email_replies"]
    assert "needs_user" in _cols(conn, "email_replies") and f"needs_{OLD}" not in _cols(conn, "email_replies")
    assert _indexes(conn, "email_replies") == {"idx_replies_deal", "idx_replies_thread"}
    with pytest.raises(sqlite3.IntegrityError):                                         # UNIQUE(message_id) survived
        conn.execute("INSERT INTO email_replies(message_id,thread_id,from_addr,received_at,body,created_at) "
                     "VALUES ('<m0@x>','t','a@b','2026-09-19','hi','t')")
    assert "_v5" not in " ".join(r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))


def test_an_unrecognisable_replies_table_refuses_rather_than_guesses(v4):
    """Two needs_* columns and no needs_user: the step cannot tell which is the old one."""
    path, before = v4
    conn = stores.engine.connect(str(path))
    conn.execute("ALTER TABLE email_replies ADD COLUMN needs_review INTEGER")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="expected one needs_"):
        stores.sales(path)
    conn = stores.engine.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert [tuple(r) for r in conn.execute("SELECT * FROM followup_decisions ORDER BY id")] == before["followup_decisions"]


def test_a_failure_half_way_leaves_the_v4_database_untouched(v4, monkeypatch):
    path, before = v4
    monkeypatch.setattr(migrate, "FOLLOWUP_DECISIONS_V5",
                        migrate.FOLLOWUP_DECISIONS_V5.replace("created_at        TEXT NOT NULL", "created_at TEXT, stage"))
    with pytest.raises(sqlite3.OperationalError):                                       # duplicate column name
        stores.sales(path)
    conn = stores.engine.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4 and not conn.in_transaction
    for table, rows in before.items():
        assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")] == rows, table
    assert f"needs_{OLD}" in _cols(conn, "email_replies")
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_v5'").fetchone()[0] == 0
    monkeypatch.undo()
    conn.close()
    conn = stores.sales(path)                                                            # then it goes through
    assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM followup_decisions WHERE decision='ask_user'").fetchone()[0] == 2


def test_a_copy_failure_rolls_back_everything(v4, monkeypatch):
    path, before = v4
    monkeypatch.setitem(migrate.FOLLOWUP_DECISIONS_V5_VALUES, "rationale", "NULL")      # NOT NULL rationale
    with pytest.raises(sqlite3.IntegrityError):
        stores.sales(path)
    conn = stores.engine.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert [tuple(r) for r in conn.execute("SELECT * FROM followup_decisions ORDER BY id")] == before["followup_decisions"]


def test_several_handles_opening_at_once_migrate_once(v4):
    path, _ = v4
    errors, barrier = [], threading.Barrier(4)

    def open_it():
        try:
            barrier.wait(timeout=10)
            conn = stores.sales(path)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION
            assert conn.execute("SELECT COUNT(*) FROM followup_decisions").fetchone()[0] == len(DECISIONS)
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
    assert conn.execute("SELECT COUNT(*) FROM email_replies").fetchone()[0] == len(REPLIES)
    assert conn.execute("SELECT COUNT(*) FROM followup_decisions WHERE stage='user'").fetchone()[0] == 2


def test_a_replies_table_that_never_had_the_column_gets_it(tmp_path):
    """An install whose email_replies was created by an early plugin SQL has no needs_* column at all
    (CREATE IF NOT EXISTS never adds columns). The migration must add it, not refuse to open the store."""
    import sqlite3
    from salescoach.store import migrate
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE email_replies (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE, thread_id TEXT,
            email_id INTEGER, deal_id TEXT, person_id TEXT, from_addr TEXT, from_name TEXT, subject TEXT,
            received_at TEXT, body TEXT, body_full TEXT, status TEXT NOT NULL DEFAULT 'new', summary TEXT,
            run_id INTEGER, error TEXT, created_at TEXT NOT NULL, reviewed_at TEXT);
        INSERT INTO email_replies(message_id, created_at) VALUES ('m1', 'now');
        PRAGMA user_version = 4;
    """)
    conn.commit()
    migrate.run(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(email_replies)")]
    assert "needs_user" in cols and "ignored_instructions" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION
    assert conn.execute("SELECT needs_user, ignored_instructions FROM email_replies").fetchone()[:] == (0, "[]")
    migrate.run(conn)                                   # idempotent
    assert [r[1] for r in conn.execute("PRAGMA table_info(email_replies)")].count("needs_user") == 1


@pytest.mark.sqlite_only          # reconcile_columns is the SQLite way of adding plugin columns; Postgres migrates
def test_plugin_tables_gain_columns_added_after_the_install_was_created(db):
    """CREATE IF NOT EXISTS never alters an existing table: the store reconciles columns on connect."""
    from salescoach.store import stores
    db.execute("CREATE TABLE old_plugin_table (id INTEGER PRIMARY KEY, kept TEXT)")
    db.execute("INSERT INTO old_plugin_table(kept) VALUES ('x')")
    db.commit()
    sql = """CREATE TABLE IF NOT EXISTS old_plugin_table (id INTEGER PRIMARY KEY, kept TEXT,
                 flag INTEGER NOT NULL DEFAULT 0, note TEXT, hard TEXT NOT NULL);"""
    added = stores.reconcile_columns(db, sql)
    assert added == ["old_plugin_table.flag", "old_plugin_table.note"]        # 'hard' has no default: reported, not added
    row = db.execute("SELECT kept, flag, note FROM old_plugin_table").fetchone()
    assert tuple(row) == ("x", 0, None)
    assert stores.reconcile_columns(db, sql) == []                              # idempotent
