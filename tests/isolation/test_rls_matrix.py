"""The row-level policies, table by table, for EVERY OWNED table (Postgres only).

For each table in tenancy.tables_of(OWNED), through the app role the code runs as:
  A inserts a row; B (another rep) reads 0, updates 0, deletes 0, and cannot insert one as A;
  A's manager reads it and cannot update, delete or insert it; another team's manager reads 0;
  an admin who manages no team reads 0; nobody bound reads 0 and cannot insert;
  A in service mode (a background duty) updates and inserts.
The row comes from tests/isolation/factories.py, which derives it from the tracked schema, so a
new OWNED table is covered the day it is classified (or fails here until a factory rule exists).
"""
import pytest

from salescoach import identity, users
from salescoach.store import db as dbmod
from salescoach.store import tenancy

import factories

pytestmark = pytest.mark.postgres_only

OWNED_TABLES = sorted(tenancy.tables_of(tenancy.OWNED))


@pytest.fixture
def org(db):
    """Two teams: A (rep, west) managed by M1; B (rep, east) managed by M2; an admin who manages nothing."""
    west, east = users.create_team(db, "West", team_id="t-west"), users.create_team(db, "East", team_id="t-east")
    people = {}
    for uid, name, role, team in (("u-a", "Asha", "rep", west), ("u-b", "Bala", "rep", east),
                                  ("u-m1", "Mani", "manager", None), ("u-m2", "Meera", "manager", None),
                                  ("u-admin", "Adi", "admin", None)):
        people[uid] = users.create(db, f"{uid}@tessel.test", name, role=role, team_id=team["id"] if team else None,
                                   user_id=uid)
    users.set_managers(db, west["id"], ["u-m1"])
    users.set_managers(db, east["id"], ["u-m2"])
    db.commit()
    return {uid: identity.Actor(uid, role=row["role"]) for uid, row in people.items()}


def _as(conn, actor):
    return identity.as_actor(conn, actor)


def _refused(conn, fn):
    """The statement is refused by a POLICY ("violates row-level security policy", SQLSTATE 42501), not
    by a unique key or a NOT NULL that would have caught the row anyway; the transaction is rolled
    back afterwards."""
    from psycopg import errors
    with pytest.raises(errors.InsufficientPrivilege):
        fn()
    conn.rollback()


@pytest.mark.parametrize("table", OWNED_TABLES)
def test_owned_table_is_isolated(db, org, table):
    a, b, m1, m2, admin = (org[k] for k in ("u-a", "u-b", "u-m1", "u-m2", "u-admin"))
    with _as(db, a):
        mine = factories.Owner(db, "u-a")
        locator = factories.insert(db, table, "u-a", mine)
        db.commit()
        assert factories.count(db, table, locator) == 1

    with _as(db, b):
        assert factories.count(db, table, locator) == 0
        assert factories.touch(db, table, locator) == 0
        assert factories.delete(db, table, locator) == 0
        db.rollback()
        _refused(db, lambda: factories.insert(db, table, "u-a", mine))       # B writing a row as A

    with _as(db, m1):                                                          # A's manager
        assert factories.count(db, table, locator) == 1
        assert factories.touch(db, table, locator) == 0
        assert factories.delete(db, table, locator) == 0
        db.rollback()
        _refused(db, lambda: factories.insert(db, table, "u-a", mine))
        assert factories.count(db, table, locator) == 1                       # still there

    with _as(db, m2):                                                          # the other team's manager
        assert factories.count(db, table, locator) == 0
    with _as(db, admin):                                                       # runs the org, sees no content
        assert factories.count(db, table, locator) == 0

    identity.bind(db, None)
    try:
        with db.as_system():                                                   # nobody bound
            assert factories.count(db, table, locator) == 0
            _refused(db, lambda: factories.insert(db, table, "u-a", mine))
    finally:
        identity.bind(db, identity.LOCAL_ACTOR)

    with _as(db, a.as_service()):                                              # A's own background duty
        assert factories.touch(db, table, locator) == 1
        second = factories.insert(db, table, "u-a", factories.Owner(db, "u-a"))    # fresh parents: no key clash
        db.commit()
        assert factories.count(db, table, second) == 1
    with _as(db, a):
        assert factories.delete(db, table, second) == 1
        db.commit()


def test_every_owned_table_has_a_factory_rule():
    """The parametrisation above covers every OWNED table; this says so in one place."""
    assert set(OWNED_TABLES) == tenancy.tables_of(tenancy.OWNED)


def test_directory_nodes_are_shared_and_owned_nodes_are_not(db, org):
    a, b = org["u-a"], org["u-b"]
    with _as(db, a):
        mine = factories.Owner(db, "u-a")
        person, account = mine.node("person"), mine.node("account")
        call = mine.node("call")
        db.commit()
    with _as(db, b):
        seen = {r[0] for r in db.execute("SELECT id FROM nodes WHERE id IN (?,?,?)", (person, account, call))}
        assert seen == {person, account}
        assert db.execute("SELECT COUNT(*) FROM people WHERE node_id=?", (person,)).fetchone()[0] == 1
        db.execute("UPDATE nodes SET title='renamed by B' WHERE id=?", (person,))          # the directory is shared
        assert db.execute("UPDATE nodes SET title='x' WHERE id=?", (call,)).rowcount == 0
        db.commit()


def test_a_disabled_user_and_a_demoted_manager_lose_access_on_the_next_query(db, org):
    a, m1 = org["u-a"], org["u-m1"]
    with _as(db, a):
        locator = factories.insert(db, "calls", "u-a", factories.Owner(db, "u-a"))
        db.commit()
    with _as(db, m1):
        assert factories.count(db, "calls", locator) == 1
    users.set_managers(db, "t-west", [])                                       # as the local admin
    db.commit()
    with _as(db, m1):
        assert factories.count(db, "calls", locator) == 0
    users.update(db, "u-a", status="disabled")
    db.commit()
    with _as(db, a):
        assert factories.count(db, "calls", locator) == 0
        _refused(db, lambda: factories.insert(db, "calls", "u-a", factories.Owner(db, "u-a")))


def test_the_actor_is_re_issued_at_every_transaction_and_follows_as_user(db, org):
    """bind_actor sets the session; _on_begin sets the transaction. A mid-transaction as_user is seen
    at once, and the previous actor is back after it, inside the same transaction."""
    a, b = org["u-a"], org["u-b"]
    with _as(db, a):
        db.execute("BEGIN")
        assert db.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == "u-a"
        with _as(db, b):
            assert db.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == "u-b"
            assert db.execute("SELECT current_setting('app.mode', true)").fetchone()[0] == "interactive"
        assert db.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] == "u-a"
        db.commit()
        with _as(db, b.as_service()):
            db.execute("INSERT INTO reconciliations(subject,verdict,created_at) VALUES ('s','v',?)", (factories.stamp(),))
            assert db.execute("SELECT current_setting('app.mode', true)").fetchone()[0] == "service"
            assert db.execute("SELECT owner_id FROM reconciliations").fetchone()[0] == "u-b"
            db.commit()
        assert db.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0] == 0    # A does not see B's


def test_users_directory_rules(db, org):
    a, b, admin = org["u-a"], org["u-b"], org["u-admin"]
    with _as(db, b):
        assert {r[0] for r in db.execute("SELECT id FROM users")} >= {"u-a", "u-b", "u-m1", "local"}
        users.update(db, "u-b", name="Bala K", signature="B.")                 # own profile: fine
        with pytest.raises(dbmod.Error):
            users.update(db, "u-b", role="admin")                              # own role: refused by the guard
        db.rollback()
        assert db.execute("UPDATE users SET name='x' WHERE id='u-a'").rowcount == 0     # someone else's row
        with pytest.raises(dbmod.Error):
            users.create(db, "c@tessel.test", "Chitra")
        db.rollback()
        with pytest.raises(dbmod.Error):
            users.create_team(db, "North")
        db.rollback()
    with _as(db, admin):
        users.update(db, "u-b", role="manager")
        made = users.create(db, "c@tessel.test", "Chitra")
        db.commit()
        assert users.get(db, made["id"])["role"] == "rep"
    with _as(db, a):
        assert users.get(db, "u-b")["role"] == "manager"


def test_state_and_user_state_rules(db, org):
    from salescoach.store.stores import get_state, get_user_state, set_state, set_user_state
    a, b, admin = org["u-a"], org["u-b"], org["u-admin"]
    with _as(db, a):
        set_user_state(db, "k", "A's")
        assert get_user_state(db, "k") == "A's"
        with pytest.raises(dbmod.Error):
            set_state(db, "org:k", "a rep may not")
        db.rollback()
        assert db.execute("UPDATE user_state SET value='x' WHERE user_id='u-b'").rowcount == 0
    with _as(db, b):
        assert get_user_state(db, "k") is None
        assert db.execute("SELECT COUNT(*) FROM user_state").fetchone()[0] == 0
    with _as(db, admin):
        set_state(db, "org:k", "an admin may")
        db.commit()
    with _as(db, a.as_service()):
        set_state(db, "sources:x:last_run", "a duty may")
        db.commit()
        assert get_state(db, "org:k") == "an admin may"
