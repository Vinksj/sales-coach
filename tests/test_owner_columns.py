"""Phase 1: every OWNED row carries its owner, and the keys two users would collide on do not.

Both backends unless marked. Postgres-only: the owner_id default reads the session setting (a missing
setting fails an owned insert), the child-row trigger copies the parent's owner and refuses a
mismatch. SQLite-only: migration 7 on a version-6 database with data.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from salescoach import identity, repo, users
from salescoach.automation import calendar
from salescoach.memory import patterns as seller_memory
from salescoach.orchestrator import bus
from salescoach.schemas.events import Event
from salescoach.sources import paste
from salescoach.store import db as dbmod
from salescoach.store import stores, tenancy
from salescoach.store.stores import get_user_state, set_user_state

A, B = identity.Actor("u-alpha", role="rep"), identity.Actor("u-beta", role="rep")
TEXT = "Asha Rao: We lose two days on every dispute.\nMe: I will send a one-page plan by Friday.\n"


def _as(conn, actor):
    return identity.as_actor(conn, actor)


@pytest.fixture(autouse=True)
def two_users(db, dialect):
    """Postgres: the row-level policies (store/pg/0003_rls.sql) give an id with no active users row no
    rows at all, so A and B exist. SQLite holds one user per file and has no policies: nothing to do."""
    if dialect == "postgres":
        for actor, name in ((A, "Alpha"), (B, "Beta")):
            users.create(db, f"{actor.user_id}@tessel.test", name, role="rep", user_id=actor.user_id)
        db.commit()


# ---- defaults and compat ------------------------------------------------------------------------

def test_owned_rows_default_to_the_acting_user_and_directory_nodes_to_nobody(db, dialect):
    deal = repo.create_deal(db, "Acme pilot")
    acct = repo.create_account(db, "Acme", ["acme.test"])
    call = repo.create_call(db, source="paste", title="t")
    db.commit()
    assert db.execute("SELECT owner_id FROM deals WHERE node_id=?", (deal,)).fetchone()[0] == "local"
    assert db.execute("SELECT owner_id FROM nodes WHERE id=?", (deal,)).fetchone()[0] == "local"
    assert db.execute("SELECT owner_id FROM nodes WHERE id=?", (acct,)).fetchone()[0] is None
    assert db.execute("SELECT owner_id FROM calls WHERE node_id=?", (call,)).fetchone()[0] == "local"
    assert db.execute("SELECT owner_id FROM events WHERE node_id=?", (call,)).fetchone()[0] == "local"
    with _as(db, B):
        other = repo.create_deal(db, "Beta deal")
        # SQLite is one seller per file: the default is 'local' whoever acts. Postgres reads the session setting.
        want = "u-beta" if dialect == "postgres" else "local"
        assert db.execute("SELECT owner_id FROM deals WHERE node_id=?", (other,)).fetchone()[0] == want
        assert db.execute("SELECT owner_id FROM nodes WHERE id=?", (other,)).fetchone()[0] == want


def test_is_me_is_derived_from_user_id(db):
    me = repo.ensure_me(db)
    row = db.execute("SELECT is_me, user_id FROM people WHERE node_id=?", (me,)).fetchone()
    assert (row["is_me"], row["user_id"]) == (1, "local")
    assert repo.ensure_me(db) == me                                   # keyed on user_id, not "the one is_me row"
    buyer = repo.create_person(db, "Asha Rao", email="asha@acme.test")
    row = db.execute("SELECT is_me, user_id FROM people WHERE node_id=?", (buyer,)).fetchone()
    assert (row["is_me"], row["user_id"]) == (0, None)
    with _as(db, B):
        theirs = repo.ensure_me(db)
    assert theirs != me
    row = db.execute("SELECT is_me, user_id FROM people WHERE node_id=?", (theirs,)).fetchone()
    assert (row["is_me"], row["user_id"]) == (1, "u-beta")
    assert repo.me_row(db)["node_id"] == me                           # back as the local user
    # any internal user is is_me=1 (buyers exclude both); the acting owner is the one with the actor's user_id
    assert {r[0] for r in db.execute("SELECT node_id FROM people WHERE is_me=1")} == {me, theirs}


def test_the_local_user_row_exists_and_follows_the_profile(db, seller_settings):
    row = users.get(db, "local")
    assert row and row["name"] == "Maya Iyer" and row["email"] == "maya@tessel.test" and row["role"] == "admin"
    assert users.active(db)[0]["id"] == "local"
    users.sync_local(db)
    assert users.get(db, "local")["extra_emails"] == ["maya@tesselops.test", "maya.iyer@gmail.com"]


def test_a_second_user_is_refused_on_sqlite_only(db, dialect):
    if dialect == "sqlite":
        with pytest.raises(users.UserError):
            users.create(db, "b@tessel.test", "Bala")
    else:
        made = users.create(db, "b@tessel.test", "Bala")
        assert users.get(db, made["id"])["email"] == "b@tessel.test"


# ---- re-scoped keys -------------------------------------------------------------------------------

def test_seller_patterns_recompute_yields_one_row_per_owner(db, fake_llm):
    from salescoach.orchestrator import context
    for actor in (A, B):
        with _as(db, actor):
            call = repo.create_call(db, source="paste", title=f"call of {actor.user_id}")
            context.save_artifact(db, call, "analysis", {"x": 1})
            db.execute("INSERT INTO seller_observations(call_id,tag,polarity,severity,contexts,evidence_turns,"
                       "evidence_quote,confidence,created_at,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (call, "avoids_budget", "weakness", "high", "[]", "[]", "q", "high", stores.now(), actor.user_id))
            # SQLite defaults every row to 'local' (one seller per file); say whose call it is on both backends
            db.execute("UPDATE calls SET owner_id=? WHERE node_id=?", (actor.user_id, call))
            seller_memory.recompute(db)
            db.commit()
    rows = []
    for actor in (A, B):                                   # each owner reads their own row (RLS on Postgres)
        with _as(db, actor):
            rows += db.execute("SELECT owner_id, tag, calls_seen FROM seller_patterns WHERE owner_id=?",
                               (actor.user_id,)).fetchall()
    assert [tuple(r) for r in rows] == [("u-alpha", "avoids_budget", 1), ("u-beta", "avoids_budget", 1)]
    with _as(db, A):
        assert seller_memory.active_priority(db) is None                 # candidate until seen on 2 calls
        assert db.execute("SELECT owner_id FROM seller_observations WHERE owner_id='u-alpha'").fetchone()


def test_learned_pattern_ids_carry_the_owner(db):
    from salescoach.learning import patterns
    with _as(db, A):
        assert patterns.pattern_id("seller", "new:foo") == "lp:seller:u:u-alpha:new:foo"
        assert patterns.parse_pattern_id("lp:seller:u:u-alpha:new:foo") == ("seller", "u-alpha", "new:foo")
    assert patterns.parse_pattern_id("lp:seller:global:new:foo") is None
    with _as(db, A):
        patterns.recompute(db)
    with _as(db, B):
        patterns.recompute(db)
    db.commit()
    owners = {r[0] for r in db.execute("SELECT DISTINCT owner_id FROM learned_patterns")}
    assert owners <= {"u-alpha", "u-beta"}


def test_two_users_dedupe_keys_in_the_same_second_both_publish(db):
    with _as(db, A):
        first = calendar.request_refresh(db)
    with _as(db, B):
        second = calendar.request_refresh(db)
    assert first and second
    rows = db.execute("SELECT entity_id, dedupe_key FROM wf_events WHERE type='CALENDAR_REFRESH_REQUESTED' "
                      "ORDER BY id").fetchall()
    assert [r["entity_id"] for r in rows] == ["user:u-alpha", "user:u-beta"]
    assert rows[0]["dedupe_key"].startswith("CAL_REFRESH:u-alpha:") and rows[1]["dedupe_key"].startswith("CAL_REFRESH:u-beta:")
    with _as(db, A):
        assert calendar.refresh_pending(db)
        assert not calendar.request_refresh(db)                       # the same user, the same minute: deduped


def test_calendar_meetings_share_an_event_id_across_owners(db):
    for actor in (A, B):
        with _as(db, actor):
            db.execute("INSERT INTO calendar_meetings(event_id,title,start_at,end_at,first_seen_at,owner_id) "
                       "VALUES (?,?,?,?,?,?)", ("evt-1", f"meeting of {actor.user_id}", "2026-09-25T10:00:00+05:30",
                                                "2026-09-25T10:30:00+05:30", stores.now(), actor.user_id))
    db.commit()
    for actor in (A, B):
        with _as(db, actor):
            assert db.execute("SELECT COUNT(*) FROM calendar_meetings WHERE event_id='evt-1' AND owner_id=?",
                              (actor.user_id,)).fetchone()[0] == 1
    from datetime import datetime
    from salescoach.automation import common
    moment = datetime(2026, 9, 25, 9, 0, tzinfo=common.IST)
    with _as(db, A):
        mine = calendar.upcoming_meetings(db, now_dt=moment)
        assert [m["title"] for m in mine] == ["meeting of u-alpha"]
        assert calendar.set_record(db, "evt-1", True)
    with _as(db, B):
        assert db.execute("SELECT record FROM calendar_meetings WHERE owner_id='u-beta'").fetchone()[0] == "no"
    with _as(db, A):
        with pytest.raises(dbmod.IntegrityError):
            db.execute("INSERT INTO calendar_meetings(event_id,first_seen_at,owner_id) VALUES ('evt-1',?,?)",
                       (stores.now(), "u-alpha"))
        db.rollback()


def test_email_replies_share_a_message_id_across_owners(db):
    for actor in (A, B):
        with _as(db, actor):
            db.execute("INSERT INTO email_replies(message_id,thread_id,from_addr,received_at,body,created_at,owner_id) "
                       "VALUES ('<m1@x>','t','a@b',?, 'hi', ?, ?)", (stores.now(), stores.now(), actor.user_id))
    db.commit()
    for actor in (A, B):
        with _as(db, actor):
            assert db.execute("SELECT COUNT(*) FROM email_replies WHERE message_id='<m1@x>' AND owner_id=?",
                              (actor.user_id,)).fetchone()[0] == 1
    with _as(db, A):
        cur = db.execute("INSERT OR IGNORE INTO email_replies(message_id,thread_id,from_addr,received_at,body,created_at,"
                         "owner_id) VALUES ('<m1@x>','t','a@b',?, 'hi', ?, ?)", (stores.now(), stores.now(), "u-alpha"))
        assert cur.rowcount == 0


def test_the_same_pasted_text_by_two_users_is_two_calls(db, dialect):
    with _as(db, A):
        first = paste.import_text(db, TEXT, "one")
        again = paste.import_text(db, TEXT, "one again")
    with _as(db, B):
        second = paste.import_text(db, TEXT, "two")
    assert first == again and second != first
    refs = {}
    for actor in (A, B):
        with _as(db, actor):
            refs.update({r["node_id"]: r["source_ref"] for r in db.execute("SELECT node_id, source_ref FROM calls")})
    assert refs[first].startswith("paste:u-alpha:") and refs[second].startswith("paste:u-beta:")
    assert refs[first].split(":")[2] == refs[second].split(":")[2]       # the same text, the same digest
    if dialect == "postgres":                      # the rows are B's too (SQLite: 'local', one seller per file)
        with _as(db, B):
            assert db.execute("SELECT owner_id FROM calls WHERE node_id=?", (second,)).fetchone()[0] == "u-beta"
            assert {r[0] for r in db.execute("SELECT owner_id FROM turns WHERE call_id=?", (second,))} == {"u-beta"}


def test_user_state_is_per_user(db):
    set_user_state(db, "automation:followups:ran_for", "2026-09-24")               # the local user
    with _as(db, B):                                       # a user's state is written as that user (RLS: own rows only)
        set_user_state(db, "automation:followups:ran_for", "2026-01-01", user_id="u-beta")
    assert get_user_state(db, "automation:followups:ran_for") == "2026-09-24"
    with _as(db, B):
        assert get_user_state(db, "automation:followups:ran_for") == "2026-01-01"
    with _as(db, A):
        assert get_user_state(db, "automation:followups:ran_for") is None
    assert db.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'automation:%'").fetchone()[0] == 0


def test_speaker_labels_are_remembered_per_user(db):
    with _as(db, A):
        users.remember_label(db, None, "Priya S.")
        assert users.remembered_labels(db) == ["Priya S."]
    with _as(db, B):
        assert users.remembered_labels(db) == []


# ---- Postgres: the setting, the trigger ------------------------------------------------------------

@pytest.mark.postgres_only
def test_pg_owner_default_is_the_session_setting_and_a_missing_one_fails(db):
    assert db.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == "local"
    with _as(db, B):
        assert db.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == "u-beta"
        db.execute("INSERT INTO nodes(id,type,title) VALUES ('deal-b','deal','B')")
        db.execute("INSERT INTO deals(node_id,name) VALUES ('deal-b','B')")
    with _as(db, B):
        assert db.execute("SELECT owner_id FROM deals WHERE node_id='deal-b'").fetchone()[0] == "u-beta"
    identity.bind(db, None)
    try:
        with pytest.raises(dbmod.NoActorBound):                           # the suite's assertion: nobody is bound
            db.execute("SELECT 1")
        with db.as_system():
            assert db.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == ""
            with pytest.raises(dbmod.Error):                              # the policy, ahead of the NOT NULL: nobody's data is written
                db.execute("INSERT INTO nodes(id,type,title) VALUES ('deal-x','deal','X')")
            db.rollback()
    finally:
        identity.bind(db, identity.LOCAL_ACTOR)


@pytest.mark.postgres_only
def test_pg_trigger_copies_the_parents_owner_and_refuses_a_mismatch(db):
    with _as(db, A):
        call = repo.create_call(db, source="paste", title="A's call")
        db.commit()
        db.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES (?,?,?,?,?)", (call, "final", 0, "me", "hi"))
        assert db.execute("SELECT owner_id FROM turns WHERE call_id=?", (call,)).fetchone()[0] == "u-alpha"
        db.commit()
    with _as(db, B):
        with pytest.raises(dbmod.IntegrityError) as exc:
            db.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES (?,?,?,?,?)", (call, "final", 1, "me", "no"))
        assert "owner mismatch" in str(exc.value)
        db.rollback()
    with _as(db, A):
        db.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES (?,?,?,?,?)", (call, "final", 1, "me", "yes"))
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM turns WHERE call_id=? AND owner_id='u-alpha'", (call,)).fetchone()[0] == 2


@pytest.mark.postgres_only
def test_pg_every_child_table_has_its_trigger(db):
    have = {r[0] for r in db.execute(
        "SELECT event_object_table FROM information_schema.triggers WHERE trigger_schema = current_schema() "
        "AND trigger_name LIKE 'trg_%_owner'")}
    assert have == set(tenancy.OWNER_PARENTS)


# ---- SQLite: migration 7 --------------------------------------------------------------------------

V6 = Path(__file__).with_name("fixtures") / "schema_v6.sql"


@pytest.mark.sqlite_only
def test_migration_7_carries_a_version_6_database_forward(tmp_path, monkeypatch, seller_settings):
    """Every owned row becomes the local user's, the one is_me row becomes the local user's person,
    the re-keyed tables keep their rows under the new keys, pattern ids are rewritten wherever they
    are stored, and the remembered me_labels move to user_speaker_labels."""
    from salescoach import config
    config.save_user("sources", {"me_labels": ["Priya S."], "sources": []})
    path = tmp_path / "v6.db"
    raw = sqlite3.connect(path)
    raw.executescript(V6.read_text())
    raw.executescript("""
        INSERT INTO nodes(id,type,title) VALUES ('acct-1','account','Acme'), ('person-me','person','Maya'),
            ('person-1','person','Asha'), ('deal-1','deal','Pilot'), ('call-1','call','Call'), ('loop-1','loop','Send deck');
        INSERT INTO accounts(node_id,name) VALUES ('acct-1','Acme');
        INSERT INTO people(node_id,name,email,is_me) VALUES ('person-me','Maya','maya@tessel.test',1), ('person-1','Asha','asha@acme.test',0);
        INSERT INTO deals(node_id,name) VALUES ('deal-1','Pilot');
        INSERT INTO calls(node_id,source,source_ref,updated_at) VALUES ('call-1','paste','paste:abc','t');
        INSERT INTO turns(call_id,tier,idx,channel,text) VALUES ('call-1','final',0,'me','hi');
        INSERT INTO loops(node_id,deal_id,call_id,type,description,owner,source,confidence,created_at)
            VALUES ('loop-1','deal-1','call-1','my_action','Send the deck','me','explicit_commitment','high','t');
        INSERT INTO seller_patterns(tag,name,polarity) VALUES ('avoids_budget','Avoids budget','weakness');
        INSERT INTO calendar_cache(key,fetched_at) VALUES ('k1','t');
        INSERT INTO calendar_meetings(event_id,title,first_seen_at) VALUES ('evt-1','Meet','t');
        INSERT INTO email_replies(message_id,thread_id,from_addr,received_at,body,created_at) VALUES ('<m1>','t','a@b','t','hi','t');
        INSERT INTO learned_patterns(id,family,key,scope,merged_into) VALUES
            ('lp:seller:global:new:foo','seller','new:foo','global','lp:seller:global:avoids_budget'),
            ('lp:seller:global:avoids_budget','seller','avoids_budget','global',NULL);
        INSERT INTO learning_proposals(kind,subject,pattern_id,target_id,summary,created_at) VALUES
            ('merge','seller:new:foo->avoids_budget','lp:seller:global:new:foo','lp:seller:global:avoids_budget','m','t');
        INSERT INTO field_provenance(entity_id,field,value,confidence,updated_at) VALUES ('lp:seller:global:new:foo','user_state','wrong','user_input','t');
        INSERT INTO coach_reports(calls_analysed,json,created_at) VALUES (1,'{}','t');
        PRAGMA user_version = 6;
    """)
    raw.commit()
    raw.close()
    monkeypatch.delenv("SALESCOACH_NO_PLUGINS", raising=False)
    conn = stores.sales(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION == 9
        for table in sorted(tenancy.tables_of(tenancy.OWNED)):
            assert "owner_id" in conn.columns(table), table
            others = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_id IS NOT 'local'").fetchone()[0]
            if table == "nodes":
                assert conn.execute("SELECT COUNT(*) FROM nodes WHERE owner_id IS NULL").fetchone()[0] == 3
                assert conn.execute("SELECT COUNT(*) FROM nodes WHERE owner_id='local'").fetchone()[0] == 3
            else:
                assert others == 0, table
        assert conn.execute("SELECT user_id, is_me FROM people WHERE node_id='person-me'").fetchone()[:] == ("local", 1)
        assert conn.execute("SELECT user_id FROM people WHERE node_id='person-1'").fetchone()[0] is None
        assert [tuple(r) for r in conn.execute("SELECT owner_id, tag FROM seller_patterns")] == [("local", "avoids_budget")]
        assert conn.execute("SELECT owner_id FROM calendar_meetings WHERE event_id='evt-1'").fetchone()[0] == "local"
        assert conn.execute("SELECT owner_id FROM calendar_cache WHERE key='k1'").fetchone()[0] == "local"
        assert conn.execute("SELECT owner_id FROM email_replies WHERE message_id='<m1>'").fetchone()[0] == "local"
        ids = {r[0]: r[1] for r in conn.execute("SELECT id, merged_into FROM learned_patterns")}
        assert ids == {"lp:seller:u:local:new:foo": "lp:seller:u:local:avoids_budget",
                       "lp:seller:u:local:avoids_budget": None}
        assert conn.execute("SELECT pattern_id, target_id FROM learning_proposals").fetchone()[:] == (
            "lp:seller:u:local:new:foo", "lp:seller:u:local:avoids_budget")
        assert conn.execute("SELECT entity_id FROM field_provenance").fetchone()[0] == "lp:seller:u:local:new:foo"
        # the new keys hold: a second owner may reuse a tag, an event id and a message id; the same owner may not
        conn.execute("INSERT INTO seller_patterns(tag,name,polarity,owner_id) VALUES ('avoids_budget','x','weakness','u-beta')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO seller_patterns(tag,name,polarity) VALUES ('avoids_budget','x','weakness')")
        conn.rollback()
        pk = {r[1] for r in conn.execute("PRAGMA index_list(seller_patterns)") if r[3] == "pk"}
        assert pk and {r[2] for r in conn.execute(f"PRAGMA index_xinfo({pk.pop()})") if r[2]} == {"owner_id", "tag"}
        assert users.get(conn, "local")["name"] == "Maya Iyer"
        assert users.remembered_labels(conn, "local") == ["Priya S."]
        assert "me_labels" not in config.load_user("sources")
        # every owner index is there, the loops(owner, status) one untouched
        names = {r[1] for r in conn.execute("PRAGMA index_list(loops)")}
        assert {"idx_loops_owner", "idx_loops_owner_id"} <= names
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE owner_id='local'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM coach_reports WHERE owner_id='local'").fetchone()[0] == 1
    finally:
        conn.close()
    again = stores.sales(path)                                        # idempotent: a second open changes nothing
    assert again.execute("PRAGMA user_version").fetchone()[0] == 9
    again.close()


def test_a_fresh_store_and_a_migrated_one_have_the_same_owner_shape(db):
    """Every OWNED table's owner_id is NOT NULL (nodes apart) with the same default on a fresh store."""
    for table in sorted(tenancy.tables_of(tenancy.OWNED)):
        info = {r["name"]: r for r in db.execute("PRAGMA table_info(%s)" % table)}
        assert "owner_id" in info, table
        assert bool(info["owner_id"]["notnull"]) == (table not in tenancy.OWNER_NULLABLE), table


def test_bus_events_carry_no_owner_column_but_the_worker_resolves_one(db, dialect):
    from salescoach.orchestrator import workflow
    with _as(db, B):
        call = repo.create_call(db, source="paste", title="B's")
        db.commit()
    assert workflow.owner_of_event(db, Event(type="CALL_ENDED", entity_id=call)) == (
        "u-beta" if dialect == "postgres" else "local")                       # SQLite: one seller per file
    assert workflow.owner_of_event(db, Event(type="FOLLOW_UP_RUN", entity_id="user:u-alpha")) == "u-alpha"
    assert workflow.owner_of_event(db, Event(type="X", payload={"owner_id": "u-gamma"})) == "u-gamma"
    assert workflow.owner_of_event(db, Event(type="X")) == "local"                 # local mode: the local user
    assert bus.publish(db, Event(type="X", entity_id="user:u-alpha", dedupe_key="X:u-alpha:1"))
    assert json.loads(db.execute("SELECT payload FROM wf_events WHERE dedupe_key='X:u-alpha:1'").fetchone()[0]) == {}
