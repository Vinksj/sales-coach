"""store/db.py: the one connection surface over SQLite and Postgres.

The translator tests run everywhere (they are pure functions). The live-connection tests run on
whichever backend the suite is on, plus a Postgres-only block for what only Postgres can show
(RETURNING id, the aborted-transaction rule, the PRAGMA table_info shim).
"""
import sqlite3

import pytest

from salescoach.store import db
from salescoach.store.db import translate

# ---- the translator ---------------------------------------------------------------------------


def test_params_qmark_and_named():
    assert translate("SELECT * FROM calls WHERE node_id=? AND deal_id=?").sql == \
        "SELECT * FROM calls WHERE node_id=%s AND deal_id=%s"
    assert translate("INSERT INTO nodes(id,type) VALUES (:id,:type)").sql == \
        "INSERT INTO nodes(id,type) VALUES (%(id)s,%(type)s)"


def test_percent_is_escaped_everywhere_and_placeholders_are_not():
    t = translate("SELECT id FROM deal_risks WHERE id NOT LIKE '%:risk:%' AND field LIKE ? AND n % 2 = 0")
    assert t.sql == "SELECT id FROM deal_risks WHERE id NOT LIKE '%%:risk:%%' AND field LIKE %s AND n %% 2 = 0"


def test_question_mark_inside_a_literal_is_text():
    assert translate("SELECT 'what?' AS q, ? AS p").sql == "SELECT 'what?' AS q, %s AS p"


def test_insert_or_ignore_becomes_on_conflict_do_nothing():
    t = translate("INSERT OR IGNORE INTO call_participants(call_id,person_id) VALUES (?,?)")
    assert t.sql.split() == "INSERT INTO call_participants(call_id,person_id) VALUES (%s,%s) ON CONFLICT DO NOTHING".split()
    assert t.is_dml and not t.auto_returning


def test_insert_or_replace_is_refused():
    with pytest.raises(db.NotSupported):
        translate("INSERT OR REPLACE INTO sources(node_id) VALUES (?)")
    with pytest.raises(db.NotSupported):
        translate("REPLACE INTO sources(node_id) VALUES (?)")


def test_returning_id_for_identity_tables_only():
    t = translate("INSERT INTO emails(call_id,kind) VALUES (?,'followup')")
    assert t.sql.endswith(" RETURNING id") and t.auto_returning
    t = translate("INSERT INTO email_replies(message_id) VALUES (?) ON CONFLICT DO NOTHING")
    assert t.sql.endswith("ON CONFLICT DO NOTHING RETURNING id")
    t = translate("INSERT INTO calls(node_id,source) VALUES (?,?)")          # TEXT primary key: no id
    assert not t.auto_returning and "RETURNING" not in t.sql
    t = translate("INSERT INTO emails(call_id) VALUES (?) RETURNING id")    # the caller's own RETURNING
    assert not t.auto_returning and t.sql.count("RETURNING") == 1


def test_is_param_is_null_safe_equality():
    t = translate("SELECT 1 FROM emails WHERE deal_id IS ? AND call_id IS NOT ?")
    assert " ".join(t.sql.split()) == "SELECT 1 FROM emails WHERE deal_id IS NOT DISTINCT FROM %s AND call_id IS DISTINCT FROM %s"
    assert translate("SELECT 1 WHERE x IS NULL AND y IS NOT NULL").sql == "SELECT 1 WHERE x IS NULL AND y IS NOT NULL"
    t = translate("SELECT 1 FROM deal_stage_history h WHERE h.from_status IS NOT h.to_status AND a IS b")
    assert " ".join(t.sql.split()) == \
        "SELECT 1 FROM deal_stage_history h WHERE h.from_status IS DISTINCT FROM h.to_status AND a IS NOT DISTINCT FROM b"


def test_order_by_makes_sqlite_null_order_explicit():
    assert translate("SELECT * FROM loops ORDER BY due_date").sql.endswith("ORDER BY due_date NULLS FIRST")
    assert translate("SELECT * FROM loops ORDER BY due_date DESC, owner_name").sql.endswith(
        "ORDER BY due_date DESC NULLS LAST, owner_name NULLS FIRST")
    assert translate("SELECT * FROM x ORDER BY MIN(id) DESC LIMIT 1").sql.endswith("ORDER BY MIN(id) DESC NULLS LAST LIMIT 1")
    assert translate("SELECT * FROM x ORDER BY CASE WHEN a=1 THEN 0 ELSE 1 END, b").sql.endswith(
        "CASE WHEN a=1 THEN 0 ELSE 1 END NULLS FIRST, b NULLS FIRST")


def test_order_by_leaves_not_null_columns_and_explicit_nulls_alone():
    assert translate("SELECT * FROM wf_events ORDER BY id LIMIT 1").sql == "SELECT * FROM wf_events ORDER BY id LIMIT 1"
    assert translate("SELECT * FROM turns t ORDER BY t.idx DESC").sql.endswith("ORDER BY t.idx DESC")
    # node_id is nullable in events, so an unqualified name cannot be trusted: the rewrite is applied
    assert translate("SELECT * FROM calls c ORDER BY c.node_id DESC").sql.endswith("ORDER BY c.node_id DESC NULLS LAST")
    assert translate("SELECT * FROM loops ORDER BY due_date NULLS LAST").sql.endswith("ORDER BY due_date NULLS LAST")
    inner = translate("SELECT (SELECT started_at FROM calls ORDER BY started_at DESC LIMIT 1) AS last, id FROM x ORDER BY id")
    assert inner.sql == "SELECT (SELECT started_at FROM calls ORDER BY started_at DESC NULLS LAST LIMIT 1) AS last, id FROM x ORDER BY id"


def test_transaction_words():
    assert translate("BEGIN IMMEDIATE").sql == "BEGIN" and translate("BEGIN").kind == "BEGIN"
    assert translate("SAVEPOINT s1").savepoint == "s1"
    assert translate("RELEASE SAVEPOINT s1").savepoint == "s1" and translate("RELEASE s1").savepoint == "s1"
    assert translate("ROLLBACK TO s1").savepoint == "s1" and translate("ROLLBACK").savepoint is None
    assert translate("COMMIT").kind == "COMMIT"


def test_statement_kind_and_dml():
    assert translate("  -- a comment\n UPDATE calls SET title=? WHERE node_id=?").kind == "UPDATE"
    assert translate("SELECT 1").is_dml is False
    assert translate("WITH x AS (SELECT 1) INSERT INTO state(key) SELECT 'k' FROM x").is_dml is True
    assert translate("WITH x AS (SELECT 1) SELECT * FROM x").is_dml is False


def test_translation_is_cached_per_statement_text():
    a = translate("SELECT ? FROM calls")
    assert translate("SELECT ? FROM calls") is a


def test_like_helper():
    assert db.like("subject") == "subject LIKE ?"
    assert db.like("p.name", ci=True) == "LOWER(p.name) LIKE LOWER(?)"


# ---- rows and cursors (the Postgres types; SQLite keeps sqlite3.Row) ----------------------------


def test_row_access_forms():
    names = ("id", "title", "deal_id")
    row = db.Row(names, (7, "NWP weekly", None))
    assert row["id"] == 7 and row["title"] == "NWP weekly" and row["deal_id"] is None
    assert row[0] == 7 and row[-1] is None and row[1:] == ("NWP weekly", None)
    assert dict(row) == {"id": 7, "title": "NWP weekly", "deal_id": None}
    assert row.keys() == ["id", "title", "deal_id"] and list(row) == [7, "NWP weekly", None]
    assert len(row) == 3
    i, t, d = row
    assert (i, t, d) == (7, "NWP weekly", None) and tuple(row) == (7, "NWP weekly", None)
    assert row["TITLE"] == "NWP weekly"                          # sqlite3.Row is case-insensitive too
    with pytest.raises(IndexError):                              # what sqlite3.Row raises ...
        row["nope"]
    with pytest.raises(KeyError):                                # ... and what dict-minded code catches
        row["nope"]
    assert row == db.Row(names, (7, "NWP weekly", None)) and row == (7, "NWP weekly", None)


def test_cursor_fetch_surface_and_lastrowid_refusal():
    names = ("n",)
    cur = db.Cursor(None, [db.Row(names, (1,)), db.Row(names, (2,)), db.Row(names, (3,))], 3, insert_id=None)
    assert cur.fetchone()["n"] == 1
    assert [r["n"] for r in cur.fetchmany(1)] == [2]
    assert [r["n"] for r in cur] == [3] and cur.fetchone() is None and cur.fetchall() == []
    with pytest.raises(db.NotSupported):
        cur.lastrowid
    assert db.insert_id(db.Cursor(None, [], 1, insert_id=42)) == 42


# ---- a live connection, on whichever backend the suite runs -------------------------------------


def test_connection_surface(db_conn):
    conn = db_conn
    assert conn.dialect in ("sqlite", "postgres")
    assert conn.table_exists("calls") and not conn.table_exists("no_such_table")
    assert conn.columns("state") == ["key", "value", "updated_at"]
    assert not conn.in_transaction
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("k", "v", "t"))
    assert conn.in_transaction                                   # DML opened one, as sqlite3 does
    conn.rollback()
    assert conn.execute("SELECT value FROM state WHERE key=?", ("k",)).fetchone() is None
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("k", "v", "t"))
    conn.commit()
    row = conn.execute("SELECT key, value FROM state WHERE key=?", ("k",)).fetchone()
    assert row["value"] == "v" and row[0] == "k" and dict(row) == {"key": "k", "value": "v"}
    assert conn.execute("SELECT 1 FROM state WHERE value IS ?", ("v",)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM state WHERE value IS ?", (None,)).fetchone() is None
    assert conn.execute("SELECT key FROM state WHERE key LIKE '%'").fetchone()[0] == "k"


def test_insert_id_and_rowcount(db_conn):
    conn = db_conn
    cur = conn.execute("INSERT INTO events(ts,kind) VALUES (?,?)", ("t", "k"))
    first = db.insert_id(cur)
    assert isinstance(first, int) and cur.rowcount == 1
    cur = conn.execute("INSERT INTO events(ts,kind) VALUES (?,?)", ("t", "k"))
    assert db.insert_id(cur) == first + 1
    conn.commit()


def test_exception_aliases_catch_the_drivers_errors(db_conn):
    conn = db_conn
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("dup", "v", "t"))
    conn.commit()
    with pytest.raises(db.IntegrityError):
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("dup", "v", "t"))
    conn.rollback()
    with pytest.raises(db.Error):
        conn.execute("SELECT * FROM no_such_table")
    conn.rollback()
    assert sqlite3.IntegrityError in db.IntegrityError          # SQLite's own errors flow through unchanged


def test_insert_or_ignore_reports_rowcount_zero_without_raising(db_conn):
    conn = db_conn
    conn.execute("INSERT INTO nodes(id,type) VALUES (?,?)", ("n1", "call"))
    conn.execute("INSERT INTO nodes(id,type) VALUES (?,?)", ("p1", "person"))
    assert conn.execute("INSERT OR IGNORE INTO call_participants(call_id,person_id) VALUES (?,?)", ("n1", "p1")).rowcount == 1
    assert conn.execute("INSERT OR IGNORE INTO call_participants(call_id,person_id) VALUES (?,?)", ("n1", "p1")).rowcount == 0
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("still", "open", "t"))   # the txn is fine
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM call_participants").fetchone()[0] == 1


def test_booleans_are_stored_as_integers(db_conn):
    conn = db_conn
    conn.execute("INSERT INTO nodes(id,type) VALUES (?,?)", ("p2", "person"))
    conn.execute("INSERT INTO people(node_id,name,is_me) VALUES (?,?,?)", ("p2", "Me", True))
    conn.commit()
    assert conn.execute("SELECT is_me FROM people WHERE node_id=?", ("p2",)).fetchone()[0] == 1


def test_savepoint_outside_a_transaction_opens_one_and_release_commits(db_conn):
    conn = db_conn
    conn.execute("SAVEPOINT sp")
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("sp", "v", "t"))
    conn.execute("RELEASE sp")
    assert not conn.in_transaction
    assert conn.execute("SELECT value FROM state WHERE key='sp'").fetchone()[0] == "v"
    conn.execute("SAVEPOINT sp2")
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("sp2", "v", "t"))
    conn.execute("ROLLBACK TO sp2")
    conn.execute("RELEASE sp2")
    assert conn.execute("SELECT value FROM state WHERE key='sp2'").fetchone() is None


def test_serialize_and_lock_rows(db_conn):
    conn = db_conn
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("lock", "v", "t"))
    conn.commit()
    conn.serialize("state:lock")
    assert conn.in_transaction
    conn.commit()
    row = conn.lock_rows("SELECT * FROM state WHERE key=?", ("lock",)).fetchone()
    assert row["value"] == "v" and conn.in_transaction
    assert conn.lock_rows("SELECT * FROM state WHERE key=?", ("absent",), skip_locked=True).fetchone() is None
    conn.commit()


def test_begin_inside_a_transaction_raises_on_both(db_conn):
    conn = db_conn
    conn.execute("BEGIN")
    with pytest.raises(db.OperationalError):
        conn.execute("BEGIN")
    conn.rollback()


def test_closed_connection_refuses_work(db_conn):
    conn = db_conn
    conn.close()
    with pytest.raises(db.ProgrammingError):
        conn.execute("SELECT 1")
    conn.close()                                                 # idempotent


@pytest.fixture
def db_conn(db):
    return db


# ---- Postgres only ------------------------------------------------------------------------------


@pytest.mark.postgres_only
def test_pg_translation_reaches_the_server(db_conn):
    conn = db_conn
    assert conn.dialect == "postgres"
    assert conn.execute("SELECT '%'").fetchone()[0] == "%"
    assert conn.execute("SELECT ? AS a", ("x",)).fetchone()["a"] == "x"
    names = ("cid", "name", "type", "notnull", "dflt_value", "pk")
    info = conn.execute("PRAGMA table_info(state)").fetchall()
    assert [r["name"] for r in info] == ["key", "value", "updated_at"]
    assert info[0].keys() == list(names) and info[0]["pk"] == 1 and info[0]["notnull"] == 1
    assert conn.execute("PRAGMA busy_timeout = 30000").fetchall() == []
    with pytest.raises(db.NotSupported):
        conn.execute("PRAGMA user_version")


@pytest.mark.postgres_only
def test_pg_failed_statement_aborts_the_transaction(db_conn):
    conn = db_conn
    """The one rule the dialect subset adds: after an error, roll back before going on."""
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("a", "v", "t"))
    with pytest.raises(db.IntegrityError):
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES (?,?,?)", ("a", "v", "t"))
    with pytest.raises(db.Error):
        conn.execute("SELECT 1")
    conn.rollback()
    assert conn.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.postgres_only
def test_pg_executescript_and_raw(db_conn):
    conn = db_conn
    conn.executescript("INSERT INTO state(key,value,updated_at) VALUES ('s1','v','t'); "
                     "INSERT INTO state(key,value,updated_at) VALUES ('s2','v','t');")
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM state").fetchone()[0] == 2
    assert conn.raw.info.transaction_status == 0
