"""A forgotten WHERE cannot leak a pipeline (Postgres only).

The code base scopes reads by owner in a few places and relies on the database everywhere else
(plan, Approach 1). Here the scoping is removed on purpose: a query helper is monkeypatched to drop
its owner clause and raw `SELECT * FROM <table>` statements run as B, and every result still holds
only B's rows.
"""
import pytest

from salescoach import identity, users
from salescoach.store import tenancy

import factories

pytestmark = pytest.mark.postgres_only

A, B = identity.Actor("u-a", role="rep"), identity.Actor("u-b", role="rep")


@pytest.fixture
def two_reps(db):
    for actor, name in ((A, "Asha"), (B, "Bala")):
        users.create(db, f"{actor.user_id}@tessel.test", name, role="rep", user_id=actor.user_id)
    db.commit()
    rows = {}
    for actor in (A, B):
        with identity.as_actor(db, actor):
            owner = factories.Owner(db, actor.user_id)
            rows[actor.user_id] = {t: factories.insert(db, t, actor.user_id, owner) for t in
                                   ("calls", "deals", "loops", "emails", "turns", "nudges", "learned_patterns")}
            users.remember_label(db, None, f"label of {actor.user_id}")
            db.commit()
    return rows


def test_a_raw_select_star_as_b_returns_only_bs_rows(db, two_reps):
    with identity.as_actor(db, B):
        for table in sorted(tenancy.tables_of(tenancy.OWNED)):
            owners = {r[0] for r in db.execute(f"SELECT owner_id FROM {table}")}
            assert owners <= {"u-b", None}, table
        calls = {r[0] for r in db.execute("SELECT node_id FROM calls")}
        assert two_reps["u-b"]["calls"]["node_id"] in calls and two_reps["u-a"]["calls"]["node_id"] not in calls
        assert {r[0] for r in db.execute("SELECT owner_id FROM nodes WHERE owner_id IS NOT NULL")} == {"u-b"}


def test_a_helper_stripped_of_its_owner_clause_still_sees_one_user(db, two_reps, monkeypatch):
    def leaky_labels(conn, user_id=None):                                     # users.remembered_labels without WHERE
        return [r[0] for r in conn.execute("SELECT label FROM user_speaker_labels ORDER BY label").fetchall()]

    monkeypatch.setattr(users, "remembered_labels", leaky_labels)
    with identity.as_actor(db, B):
        assert users.remembered_labels(db) == ["label of u-b"]
    with identity.as_actor(db, A):
        assert users.remembered_labels(db) == ["label of u-a"]

    def leaky_get_call(conn, call_id):                                        # repo.get_call by id, any owner
        return conn.execute("SELECT * FROM calls WHERE node_id=?", (call_id,)).fetchone()

    from salescoach import repo
    monkeypatch.setattr(repo, "get_call", leaky_get_call)
    with identity.as_actor(db, B):
        assert repo.get_call(db, two_reps["u-a"]["calls"]["node_id"]) is None
        assert repo.get_call(db, two_reps["u-b"]["calls"]["node_id"]) is not None


def test_a_join_across_owners_leaks_nothing(db, two_reps):
    with identity.as_actor(db, B):
        rows = db.execute("SELECT c.node_id, t.text, e.subject FROM calls c JOIN turns t ON t.call_id = c.node_id "
                          "LEFT JOIN emails e ON e.call_id = c.node_id").fetchall()
        assert {r[0] for r in rows} == {two_reps["u-b"]["turns"]["call_id"]}
        assert two_reps["u-a"]["turns"]["call_id"] not in {r[0] for r in rows}
        assert db.execute("SELECT COUNT(*) FROM learned_patterns WHERE id LIKE 'lp:%'").fetchone()[0] == 1


def test_an_owner_cannot_be_moved_by_update(db, two_reps):
    with identity.as_actor(db, B):
        call = two_reps["u-b"]["calls"]["node_id"]
        with pytest.raises(Exception):                                         # WITH CHECK: the row would leave B
            db.execute("UPDATE calls SET owner_id='u-a' WHERE node_id=?", (call,))
        db.rollback()
        assert db.execute("UPDATE calls SET owner_id='u-b' WHERE node_id=?", (call,)).rowcount == 1
        db.commit()
