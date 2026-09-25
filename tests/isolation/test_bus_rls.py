"""The bus (wf_events) under row-level security (Postgres, the app role).

Before: every signed-in session could read, insert, update and delete every wf_events row, error text and
events filed under another user's name included (store/rls.py had wf_events open: the worker claims with
nobody bound, so the table could not be scoped by owner). Now:

  * a user session (interactive or service) reads and updates its OWN events only; a manager reads none of
    the team's; nobody deletes one;
  * a session inserts an event only as its owner, and only one whose entity and payload name that owner (the
    worker runs an event as the owner its entity names);
  * the worker's claim and settle paths run with nobody bound, through the app_bus_* functions, which refuse
    any session with an actor bound and take or settle an event only under its owner's lock;
  * the nav's queued / failed counts are the viewer's own.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg import errors

from salescoach import identity, users
from salescoach.orchestrator import bus, worker, workflow
from salescoach.schemas.events import Event
from salescoach.store import stores
from salescoach.store.stores import now
from salescoach.web import app as app_module

import factories
from conftest import seed_org_settings
from test_route_crawl import cloud  # noqa: F401

pytestmark = pytest.mark.postgres_only

A, B, M, D = "u-a", "u-b", "u-m", "u-d"
ROLES = {A: "rep", B: "rep", M: "manager", D: "admin"}


def _as(db, uid, mode=identity.INTERACTIVE):
    return identity.as_actor(db, identity.Actor(uid, mode=mode, role=ROLES[uid]))


@pytest.fixture
def org(db):
    """Reps A and B on one team managed by M, and an admin D, made by the local admin."""
    users.create_team(db, "West", team_id="t-west")
    for uid, role in ROLES.items():
        users.create(db, f"{uid}@tessel.test", uid, role=role, team_id="t-west" if role == "rep" else None, user_id=uid)
    users.set_managers(db, "t-west", [M])
    db.commit()
    seed_org_settings(db)
    return True


@pytest.fixture
def a_call(db, org):
    with _as(db, A):
        call = factories.insert(db, "calls", A, factories.Owner(db, A))["node_id"]
        db.commit()
    return call


def _publish_as(db, uid, event, mode=identity.INTERACTIVE):
    with _as(db, uid, mode):
        ok = bus.publish(db, event)
        db.commit()
    return ok


def _raw_insert(db, owner, entity, payload="{}"):
    db.execute("INSERT INTO wf_events(event_id,type,entity_id,payload,dedupe_key,status,created_at,priority,owner) "
               "VALUES (?,?,?,?,?,'pending',?,0,?)", (uuid.uuid4().hex, "T", entity, payload, uuid.uuid4().hex, now(), owner))


def _refused(db, fn):
    with pytest.raises(errors.InsufficientPrivilege):
        fn()
    db.rollback()


def test_a_rep_reads_updates_and_deletes_none_of_another_reps_events(db, org, a_call, pg_owner):
    ev = Event(type="CALL_ENDED", entity_id=a_call, dedupe_key="iso:1")
    assert _publish_as(db, A, ev)
    pg_owner.execute("UPDATE wf_events SET status='failed', error='secret stack trace' WHERE event_id=?", (ev.event_id,))
    pg_owner.commit()
    with _as(db, B):
        assert db.execute("SELECT COUNT(*) FROM wf_events").fetchone()[0] == 0
        assert db.execute("UPDATE wf_events SET status='pending', attempts=0, error=NULL").rowcount == 0
        assert db.execute("DELETE FROM wf_events").rowcount == 0
        db.commit()
    with _as(db, M):                                   # A's manager reads A's calls, not A's bus
        assert db.execute("SELECT COUNT(*) FROM wf_events").fetchone()[0] == 0
    with _as(db, A):                                   # A's own: readable, retryable, not deletable
        assert db.execute("SELECT error FROM wf_events").fetchone()[0] == "secret stack trace"
        assert db.execute("UPDATE wf_events SET status='pending', attempts=0, error=NULL").rowcount == 1
        assert db.execute("DELETE FROM wf_events").rowcount == 0
        db.commit()
    row = pg_owner.execute("SELECT status, error FROM wf_events WHERE event_id=?", (ev.event_id,)).fetchone()
    assert (row["status"], row["error"]) == ("pending", None)


def test_nobody_files_an_event_under_another_owner(db, org, a_call):
    with _as(db, B):
        _refused(db, lambda: _raw_insert(db, A, f"user:{A}"))                 # A's name on B's insert
        _refused(db, lambda: _raw_insert(db, B, a_call))                      # B's name, A's call
        _refused(db, lambda: _raw_insert(db, B, f"user:{A}"))
        _refused(db, lambda: _raw_insert(db, B, None, json.dumps({"owner_id": A})))
        _refused(db, lambda: _raw_insert(db, None, None))                      # nobody's
    with _as(db, B, identity.SERVICE):                  # bus.publish refuses only interactive sessions; the table, all
        _refused(db, lambda: bus.publish(db, Event(type="CALL_ENDED", entity_id=a_call, dedupe_key="iso:svc")))
    with _as(db, B):                                   # B's own work is B's to queue
        _raw_insert(db, B, f"user:{B}")
        db.commit()
    assert _publish_as(db, A, Event(type="CALL_ENDED", entity_id=a_call, dedupe_key="iso:own"))
    with _as(db, A):                                   # nor retargets an own event at someone else's work
        _refused(db, lambda: db.execute("UPDATE wf_events SET entity_id=? WHERE dedupe_key='iso:own'", (f"user:{B}",)))
        _refused(db, lambda: db.execute("UPDATE wf_events SET owner=? WHERE dedupe_key='iso:own'", (B,)))


def test_a_user_session_cannot_use_the_machinery(db, org, a_call):
    ev = Event(type="CALL_ENDED", entity_id=a_call, dedupe_key="iso:m")
    assert _publish_as(db, A, ev)
    for uid, mode in ((B, identity.INTERACTIVE), (B, identity.SERVICE), (A, identity.INTERACTIVE), (D, identity.INTERACTIVE)):
        with _as(db, uid, mode):
            for sql, params in (("SELECT * FROM app_bus_candidates(3, ?, 50)", (now(),)),
                                ("SELECT * FROM app_bus_take(1, ?)", (now(),)),
                                ("SELECT * FROM app_bus_peek(?)", (ev.event_id,)),
                                ("SELECT * FROM app_bus_running()", ()),
                                ("SELECT app_bus_settle(?, 'done', NULL, false, false, NULL, ?, false)", (ev.event_id, now()))):
                _refused(db, lambda: db.execute(sql, params).fetchall())
    with _as(db, B):                                   # the per-owner counts: an admin or the machinery only
        _refused(db, lambda: db.execute("SELECT * FROM app_bus_depth()").fetchall())
    with _as(db, D):
        assert {r[0] for r in db.execute("SELECT * FROM app_bus_depth()").fetchall()} == {A}


def test_the_machinery_takes_and_settles_only_under_the_owners_lock(db, org, a_call, pg_owner):
    ev = Event(type="CALL_ENDED", entity_id=a_call, dedupe_key="iso:lock")
    assert _publish_as(db, A, ev)
    row_id = pg_owner.execute("SELECT id FROM wf_events WHERE event_id=?", (ev.event_id,)).fetchone()[0]
    with identity.activate(None):
        conn = stores.sales()
    try:
        with conn.as_system():
            _refused(conn, lambda: conn.execute("SELECT * FROM app_bus_take(?::integer, ?)", (row_id, now())).fetchall())
            assert bus.claim_next(conn).event_id == ev.event_id          # the claimer takes A's lock first
        other = stores.sales()
        try:
            with bus._machinery(other):                                   # a second worker, without A's lock
                _refused(other, lambda: other.execute(
                    "SELECT app_bus_settle(?, 'done', NULL, false, false, NULL, ?, false)", (ev.event_id, now())).fetchall())
        finally:
            other.close()
        bus.complete(conn, ev.event_id)
    finally:
        bus.release_owner_locks(conn)
        conn.close()
    assert pg_owner.execute("SELECT status FROM wf_events WHERE event_id=?", (ev.event_id,)).fetchone()[0] == "done"


def test_the_worker_claims_and_completes_every_owners_events(db, org, a_call, monkeypatch, pg_owner):
    """A's and B's events, each queued by its owner; one worker connection with nobody bound handles both, each
    as its owner (service mode), and settles both."""
    seen = []
    monkeypatch.setitem(workflow.HANDLERS, "T", [lambda conn, ev: seen.append(
        (ev.entity_id, conn.actor.user_id, conn.actor.mode,
         conn.execute("SELECT COUNT(*) FROM wf_events").fetchone()[0]))])
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    assert _publish_as(db, A, Event(type="T", entity_id=f"user:{A}", dedupe_key="iso:wa"))
    assert _publish_as(db, B, Event(type="T", entity_id=f"user:{B}", dedupe_key="iso:wb"), mode=identity.SERVICE)
    with identity.activate(None):
        conn = stores.sales()
    try:
        conn.system = True                              # as orchestrator/worker.Worker.run
        bus.recover_running(conn)
        assert worker.drain(conn) == 2
    finally:
        bus.release_owner_locks(conn)
        conn.close()
    assert sorted(seen) == [(f"user:{A}", A, identity.SERVICE, 1), (f"user:{B}", B, identity.SERVICE, 1)]
    assert [r[0] for r in pg_owner.execute("SELECT status FROM wf_events ORDER BY id").fetchall()] == ["done", "done"]


def test_the_nav_counts_only_the_viewers_own_events(db, org, a_call, cloud, pg_owner):  # noqa: F811
    for i in range(2):
        assert _publish_as(db, A, Event(type="PROCESS_CALL", entity_id=a_call, dedupe_key=f"iso:nav{i}"))
    pg_owner.execute("UPDATE wf_events SET status='failed' WHERE dedupe_key='iso:nav1'")
    pg_owner.commit()
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    app.state.gmail_factory = lambda: None
    client = TestClient(app, follow_redirects=False)
    mine = client.get("/", headers={"x-test-user": A, "accept": "text/html"}).text
    theirs = client.get("/", headers={"x-test-user": B, "accept": "text/html"}).text
    assert "1 workflow events queued, 1 failed" in mine
    assert "0 workflow events queued, 0 failed" in theirs
