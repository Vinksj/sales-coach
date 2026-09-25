"""Retention under row-level security (Phase 8, Postgres only).

  * rep A's retention round deletes A's expired call and nothing of rep B's (B's expired call waits for B's own
    round), and the manager's comment and view on A's call go with it (app_forget_annotations);
  * only a service session may forget annotations: A at the keyboard cannot erase a manager's comment;
  * the scheduler's round runs every active user's retention in that user's own service session.
"""
from datetime import datetime, timedelta, timezone

import pytest

from salescoach import identity, users
from salescoach.automation import scheduler
from salescoach.lifecycle import retention
from salescoach.store.stores import now

pytestmark = pytest.mark.postgres_only

A, B, M = "u-ra", "u-rb", "u-rm"
OLD = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")


@pytest.fixture
def org(db):
    users.create_team(db, "West", team_id="t-west")
    for uid, role, team in ((A, "rep", "t-west"), (B, "rep", "t-west"), (M, "manager", None)):
        users.create(db, f"{uid}@tessel.test", uid, role=role, team_id=team, user_id=uid)
    users.set_managers(db, "t-west", [M])
    db.commit()
    return db


def _as(db, uid, role="rep", mode=identity.INTERACTIVE):
    return identity.as_actor(db, identity.Actor(uid, mode, role))


def _call(db, owner, nid, started):
    with _as(db, owner):
        db.execute("INSERT INTO nodes(id,type,title) VALUES (?,'call','c')", (nid,))
        db.execute("INSERT INTO calls(node_id,source,title,started_at,wf_state) VALUES (?,'paste','c',?,'done')",
                   (nid, started))
        db.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES (?,'final',0,'me','Hello')", (nid,))
        db.commit()


def _all(db, pg_owner, sql, params=()):
    return pg_owner.execute(sql, params).fetchone()[0]


def test_a_reps_round_takes_their_expired_call_and_the_managers_notes_on_it(org, pg_owner):
    db = org
    _call(db, A, "call-ra-old", OLD)
    _call(db, A, "call-ra-new", now())
    _call(db, B, "call-rb-old", OLD)
    with _as(db, M, role="manager"):
        db.execute("INSERT INTO comments(owner_id,author_id,entity_type,entity_id,body,created_at) "
                   "VALUES (?,?,'call','call-ra-old','Ask for the CFO',?)", (A, M, now()))
        db.execute("INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) "
                   "VALUES (?,?,'call','call-ra-old',?)", (M, A, now()))
        db.commit()
    with _as(db, A, mode=identity.SERVICE):
        result = retention.purge(db, days=365)
    assert result["calls"] == 1 and result["comments_and_views"] == 2
    assert _all(db, pg_owner, "SELECT COUNT(*) FROM calls WHERE node_id='call-ra-old'") == 0
    assert _all(db, pg_owner, "SELECT COUNT(*) FROM turns WHERE call_id='call-ra-old'") == 0
    assert _all(db, pg_owner, "SELECT COUNT(*) FROM comments") == 0
    assert _all(db, pg_owner, "SELECT COUNT(*) FROM access_log") == 0
    assert _all(db, pg_owner, "SELECT COUNT(*) FROM calls WHERE node_id='call-ra-new'") == 1
    assert _all(db, pg_owner, "SELECT COUNT(*) FROM calls WHERE node_id='call-rb-old'") == 1      # B's own round
    audit = pg_owner.execute("SELECT owner_id FROM events WHERE kind='retention.purge'").fetchall()
    assert [r[0] for r in audit] == [A]


def test_only_a_service_session_forgets_annotations(org, pg_owner):
    db = org
    _call(db, A, "call-ra-old", OLD)
    with _as(db, M, role="manager"):
        db.execute("INSERT INTO comments(owner_id,author_id,entity_type,entity_id,body,created_at) "
                   "VALUES (?,?,'call','call-ra-old','Keep this',?)", (A, M, now()))
        db.commit()
    with _as(db, A):
        with pytest.raises(Exception, match="only a background duty"):
            db.execute("SELECT app_forget_annotations('call', ?)", (["call-ra-old"],))
        db.rollback()
        # and a rep's own delete cannot remove a manager's comment either (author only)
        assert db.execute("DELETE FROM comments WHERE entity_id='call-ra-old'").rowcount == 0
        db.commit()
    assert _all(db, pg_owner, "SELECT COUNT(*) FROM comments") == 1


def test_the_scheduler_round_runs_each_reps_retention_as_that_rep(org, pg_owner, monkeypatch):
    from salescoach import config
    db = org
    pg_owner.execute("INSERT INTO org_settings(name,body,version) VALUES ('org', ?, 1)",   # what Settings stores
                     ('{"retention": {"days": 365}}',))
    pg_owner.commit()
    config._org_cache.clear()
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    _call(db, A, "call-ra-old", OLD)
    _call(db, B, "call-rb-old", OLD)
    duty = next(d for d in scheduler.default_duties() if d.name == "retention")

    class Once:
        n = 0

        def wait(self, _delay):
            self.n += 1
            return self.n > 1

        def is_set(self):
            return False
    scheduler._loop(duty, None, Once())
    assert _all(db, pg_owner, "SELECT COUNT(*) FROM calls") == 0
    owners = {r[0] for r in pg_owner.execute("SELECT owner_id FROM events WHERE kind='retention.purge'").fetchall()}
    assert owners == {A, B}
    kept = pg_owner.execute("SELECT user_id FROM user_state WHERE key='automation:retention:last_run'").fetchall()
    assert {r[0] for r in kept} >= {A, B}
