"""The keys two reps could collide on (Phase 8): pattern_observations UNIQUE(owner_id, family, key, subject),
the one-open-proposal index idx_lprop_open (owner_id, kind, subject), and the reply loop id.

Before migration 12 / 0008 the first two keys named no owner, so on Postgres rep A's row (invisible to B)
made B's insert of the same family/key/subject, or of an open proposal on the same subject, fail; and a
manager's own learning sync saw the team's observations through the read policy and treated them as its own.
"""
import pytest

from salescoach import identity, users
from salescoach.automation import replies
from salescoach.learning import observe, patterns
from salescoach.store.stores import now

pytestmark = pytest.mark.postgres_only

A, B, M = "u-ka", "u-kb", "u-km"


@pytest.fixture
def org(db):
    users.create_team(db, "West", team_id="t-west")
    users.create(db, "ka@tessel.test", "Asha", role="rep", team_id="t-west", user_id=A)
    users.create(db, "kb@tessel.test", "Bala", role="rep", team_id="t-west", user_id=B)
    users.create(db, "km@tessel.test", "Mani", role="manager", user_id=M)
    users.set_managers(db, "t-west", [M])
    db.commit()
    return db


def _as(db, uid, role="rep", mode=identity.SERVICE):
    return identity.as_actor(db, identity.Actor(uid, mode, role))


def _observe(db, owner):
    db.execute("INSERT INTO pattern_observations(family,key,subject,evidence,created_at,owner_id) "
               "VALUES ('seller','talks_too_much','call:shared','{}',?,?)", (now(), owner))
    db.commit()


def test_two_reps_keep_their_own_observation_of_the_same_family_key_and_subject(org):
    db = org
    for uid in (A, B):
        with _as(db, uid):
            _observe(db, uid)                             # B's insert no longer collides with A's hidden row
    for uid in (A, B):
        with _as(db, uid):
            rows = db.execute("SELECT owner_id FROM pattern_observations WHERE family='seller' "
                              "AND key='talks_too_much' AND subject='call:shared'").fetchall()
            assert [r["owner_id"] for r in rows] == [uid]
    with _as(db, A):                                      # the key still holds within one owner
        with pytest.raises(Exception):
            _observe(db, A)
        db.rollback()


def test_a_managers_own_sync_leaves_the_teams_observations_alone(org):
    db = org
    with _as(db, A):
        _observe(db, A)
        db.execute("INSERT INTO nodes(id,type,title,owner_id) VALUES ('call-ka-1','call','A call',?)", (A,))
        db.execute("INSERT INTO calls(node_id,source,title,started_at,wf_state,owner_id) "
                   "VALUES ('call-ka-1','paste','A call','2026-09-20T10:00:00+05:30','done',?)", (A,))
        db.execute("INSERT INTO seller_observations(call_id,tag,polarity,severity,confidence,created_at,owner_id) "
                   "VALUES ('call-ka-1','talks_too_much','weakness','high','high',?,?)", (now(), A))
        db.commit()
    with _as(db, M, role="manager", mode=identity.INTERACTIVE):     # reviewing: the team's rows are readable
        assert db.execute("SELECT COUNT(*) FROM pattern_observations WHERE owner_id=?", (A,)).fetchone()[0] == 1
    with _as(db, M, role="manager"):                                 # the manager's own learning duty
        assert db.execute("SELECT COUNT(*) FROM seller_observations").fetchone()[0] == 0
        result = observe.sync(db)                    # before Phase 8 it inserted A's call as the manager's: refused
        db.commit()
        assert result == {"added": 0, "updated": 0, "removed": 0, "rows": 0}
    with _as(db, A):
        assert db.execute("SELECT COUNT(*) FROM pattern_observations").fetchone()[0] == 1


def test_two_reps_each_open_a_proposal_on_the_same_subject(org):
    db = org
    for uid in (A, B):
        with _as(db, uid):
            opened = patterns._open_proposal(db, "merge", "new:asked_for_budget", f"lp:seller:u:{uid}:new:x",
                                             f"lp:seller:u:{uid}:budget", "Merge?", {}, 3)
            db.commit()
            assert opened == 1
            # the same owner cannot open a second one while the first is open
            assert patterns._open_proposal(db, "merge", "new:asked_for_budget", None, None, "Merge?", {}, 9) == 0
    for uid in (A, B):
        with _as(db, uid):
            rows = db.execute("SELECT owner_id, status FROM learning_proposals WHERE subject='new:asked_for_budget'").fetchall()
            assert [(r["owner_id"], r["status"]) for r in rows] == [(uid, "open")]


def test_the_open_proposal_index_is_per_owner(org, pg_owner):
    cols = pg_owner.execute("SELECT indexdef FROM pg_indexes WHERE indexname='idx_lprop_open' "
                            "AND schemaname = current_schema()").fetchone()[0]
    assert "(owner_id, kind, subject)" in cols and "status = 'open'" in cols


def test_a_reply_loop_id_names_its_owner_outside_the_local_install():
    ids = {replies.reply_loop_id(uid, "<m1@buyer.test>", "We will send the PO") for uid in (A, B)}
    assert len(ids) == 2
    # the local install's ids are what they always were (loops already stored keep their id)
    import hashlib
    from salescoach.validators import evidence
    legacy = "loop-r-" + hashlib.sha1(f"<m1@buyer.test>|{evidence.normalize('We will send the PO')}".encode()).hexdigest()[:12]
    assert replies.reply_loop_id(identity.LOCAL_USER, "<m1@buyer.test>", "We will send the PO") == legacy
