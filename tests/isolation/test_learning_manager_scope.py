"""The learning recompute and proposals under the row-level policies (Postgres, cloud mode).

A manager's interactive session reads the team's rows. POST /learning/recompute ran the learning collectors
there without an owner filter and stored what it found as the MANAGER's rows: a rep's sent email became a
derived_outcomes row owned by the manager, a rep's seller observation a pattern observation of the manager's,
and because derived_outcomes was UNIQUE(kind, subject_type, subject_id) the rep's own recompute then failed on
a row the rep could not see. Now every collector reads only the acting user's rows, the recompute runs under
the actor's SERVICE binding, and the key includes owner_id (migration 0009 / SQLite 13).

Accepting a trigger-weight proposal wrote config.save_user("live_coach"), which in cloud mode is the ORG's
settings row: a 500 for a rep, an org-wide change for an admin. It is refused with a message now.
"""
import json
from datetime import timedelta

import pytest

from salescoach import identity
from salescoach.learning import patterns as learning_patterns
from salescoach.plugins import learning as plugin
from salescoach.store.stores import now
from salescoach.web import app as app_module

from test_route_crawl import cloud  # noqa: F401
from test_manager_review import A, D, M, _as, client, get, mk_call, mk_email, org, post  # noqa: F401

pytestmark = pytest.mark.postgres_only


def test_a_managers_recompute_copies_nothing_of_the_team_and_the_rep_recomputes_fine(client, db, pg_owner):
    with _as(db, A):
        day = (app_module.today_ist() - timedelta(days=20)).isoformat()
        call = mk_call(db, A, "Asha: Northwind", day, state="done")
        mk_email(db, A, call, "sent", sent_at=f"{day}T12:00:00+05:30")
        db.execute("INSERT INTO seller_observations(call_id,tag,polarity,severity,evidence_quote,confidence,created_at) "
                   "VALUES (?,'talks_too_much','weakness','high','we are the best','high',?)", (call, now()))
        db.commit()
    email_id = pg_owner.execute("SELECT id FROM emails WHERE owner_id=?", (A,)).fetchone()[0]

    r = post(client, M, "/learning/recompute")
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    for table in ("derived_outcomes", "pattern_observations", "learned_patterns"):
        assert pg_owner.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_id=?", (M,)).fetchone()[0] == 0, table

    with identity.as_user(db, A, mode=identity.SERVICE):
        result = plugin.run_recompute(db, trigger="daily")
    assert "error" not in result, result
    rows = pg_owner.execute("SELECT owner_id FROM derived_outcomes WHERE subject_type='email' AND subject_id=?",
                            (str(email_id),)).fetchall()
    assert rows and {r[0] for r in rows} == {A}
    assert pg_owner.execute("SELECT COUNT(*) FROM pattern_observations WHERE owner_id=? AND family='seller'",
                            (A,)).fetchone()[0] == 1
    # the manager again, after the rep: still nothing of A's under M, and A's rows unchanged
    assert "err=" not in post(client, M, "/learning/recompute").headers["location"]
    assert pg_owner.execute("SELECT COUNT(*) FROM derived_outcomes WHERE owner_id=?", (M,)).fetchone()[0] == 0
    # a rep's own POST works the same
    r = post(client, A, "/learning/recompute")
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]


def test_the_owner_key_lets_the_rep_upsert_past_a_stray_row_of_another_owner(db, org, pg_owner):
    """A row a manager's recompute left before the fix (owner M, about A's email) no longer blocks A."""
    with _as(db, A):
        day = (app_module.today_ist() - timedelta(days=20)).isoformat()
        call = mk_call(db, A, "Asha: Eastline", day, state="done")
        mk_email(db, A, call, "sent", sent_at=f"{day}T12:00:00+05:30")
        db.commit()
    email_id = pg_owner.execute("SELECT id FROM emails WHERE owner_id=?", (A,)).fetchone()[0]
    pg_owner.execute("INSERT INTO derived_outcomes(kind,subject_type,subject_id,value,computed_at,owner_id) "
                     "VALUES ('email_replied','email',?,0,?,?)", (str(email_id), now(), M))
    pg_owner.commit()
    with identity.as_user(db, A, mode=identity.SERVICE):
        assert "error" not in plugin.run_recompute(db, trigger="daily")
    assert pg_owner.execute("SELECT COUNT(*) FROM derived_outcomes WHERE owner_id=? AND subject_id=?",
                            (A, str(email_id))).fetchone()[0] >= 1
    with identity.as_user(db, M, mode=identity.SERVICE):              # M's own next recompute prunes the stray row
        assert "error" not in plugin.run_recompute(db, trigger="daily")
    assert pg_owner.execute("SELECT COUNT(*) FROM derived_outcomes WHERE owner_id=?", (M,)).fetchone()[0] == 0


@pytest.mark.parametrize("who", [A, D])
def test_accepting_a_trigger_weight_proposal_is_refused_cleanly_and_changes_no_org_setting(client, db, pg_owner, who):
    with _as(db, who):
        db.execute("INSERT INTO learning_proposals(kind,subject,pattern_id,summary,payload,status,created_at) "
                   "VALUES ('trigger_weight','trigger:dig_deeper',?,'Lower it',?,'open',?)",
                   (f"lp:nudge_trigger:u:{who}:dig_deeper", json.dumps({"trigger": "dig_deeper", "proposed_weight": 0.5,
                                                                        "current_weight": 1.0}), now()))
        db.commit()
        proposal = db.execute("SELECT id FROM learning_proposals WHERE owner_id=?", (who,)).fetchone()[0]
    before = pg_owner.execute("SELECT COUNT(*) FROM org_settings WHERE name='live_coach'").fetchone()[0]
    r = post(client, who, f"/learning/proposals/{proposal}/accept")
    assert r.status_code == 303 and "err=" in r.headers["location"], (r.status_code, r.headers.get("location"))
    assert pg_owner.execute("SELECT COUNT(*) FROM org_settings WHERE name='live_coach'").fetchone()[0] == before == 0
    assert pg_owner.execute("SELECT status FROM learning_proposals WHERE id=?", (proposal,)).fetchone()[0] == "open"
    r = post(client, who, f"/learning/proposals/{proposal}/dismiss")               # and it can be closed
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    assert pg_owner.execute("SELECT status FROM learning_proposals WHERE id=?", (proposal,)).fetchone()[0] == "dismissed"
    assert post(client, M, f"/learning/proposals/{proposal}/accept").status_code in (403, 404)


def test_cloud_mode_opens_no_trigger_weight_proposal(db, org, cloud, monkeypatch):
    monkeypatch.setattr(learning_patterns, "resolved_weights", lambda: {"dig_deeper": 1.0})
    with _as(db, A):
        assert learning_patterns._propose_trigger_weights(db) == 0
