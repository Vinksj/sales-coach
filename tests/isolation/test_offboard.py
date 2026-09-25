"""Offboarding (Phase 8, Postgres only, cloud mode): a rep leaves.

The org: team West (reps A and B) managed by M; team East (rep C) managed by N; D is an admin.
  * reassign A -> C: every OWNED row of A becomes C's (one row of EVERY OWNED table, from the factories, so a
    new table cannot be forgotten), except what describes A (tenancy.PERSONAL, the coaching notes about A,
    A's user_state and speaker labels), which is deleted; M's comment on A's call moves with the call and
    keeps M as its author; N (C's manager) now reads the moved call and M (A's manager, not C's) no longer
    does; A is disabled, signed out and their Google grant revoked; audit events are written;
  * purge A: every OWNED row of A is gone; the shared directory stays; B's work is untouched;
  * refusals: a rep or a manager cannot call app_offboard; an admin cannot offboard themselves, hand work to a
    non-rep or a disabled rep; the admin page wants the email typed to confirm.
"""
import json

import pytest
from fastapi.testclient import TestClient

from salescoach import identity, sessions, users
from salescoach.lifecycle import offboard
from salescoach.store import tenancy
from salescoach.store.stores import now, set_user_state
from salescoach.web import app as app_module

import factories
from conftest import seed_org_settings
from test_route_crawl import ORIGIN, cloud  # noqa: F401

pytestmark = pytest.mark.postgres_only

A, B, C, M, N, D = "u-oa", "u-ob", "u-oc", "u-om", "u-on", "u-od"
ROLES = {A: "rep", B: "rep", C: "rep", M: "manager", N: "manager", D: "admin"}


@pytest.fixture
def org(db, cloud, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "test-secret-" + "x" * 32)
    with identity.as_actor(db, identity.LOCAL_ACTOR):
        users.create_team(db, "West", team_id="t-west")
        users.create_team(db, "East", team_id="t-east")
        teams = {A: "t-west", B: "t-west", C: "t-east"}
        for uid, role in ROLES.items():
            users.create(db, f"{uid}@tessel.test", uid.upper(), role=role, team_id=teams.get(uid), user_id=uid)
        users.set_managers(db, "t-west", [M])
        users.set_managers(db, "t-east", [N])
        db.commit()
    return db


def _as(db, uid, mode=identity.INTERACTIVE):
    return identity.as_actor(db, identity.Actor(uid, mode, ROLES[uid]))


def _work(db, owner):
    """One row of every OWNED table for `owner` (as the owner), a manager's comment and a coaching note, the
    owner's user_state and speaker label and a live session. Returns the call the comment is on."""
    with _as(db, owner):
        mine = factories.Owner(db, owner)
        for table in sorted(tenancy.tables_of(tenancy.OWNED)):
            factories.insert(db, table, owner, mine)
        call = mine.parent("calls")["node_id"]
        set_user_state(db, "setup:card_dismissed", "1")
        users.remember_label(db, owner, f"{owner} label")
        db.commit()
    manager = M if owner in (A, B) else N
    with _as(db, manager):
        db.execute("INSERT INTO comments(owner_id,author_id,entity_type,entity_id,body,created_at) "
                   "VALUES (?,?,'call',?,'Ask for the CFO',?)", (owner, manager, call, now()))
        db.execute("INSERT INTO comments(owner_id,author_id,entity_type,entity_id,body,created_at) "
                   "VALUES (?,?,'coaching',?,'Slow down on pricing',?)", (owner, manager, owner, now()))
        db.execute("INSERT INTO access_log(viewer_id,owner_user_id,entity_type,entity_id,viewed_at) "
                   "VALUES (?,?,'call',?,?)", (manager, owner, call, now()))
        db.commit()
    with identity.activate(None), db.as_system():
        sessions.create(db, owner)
    return call


def _owned(pg, owner) -> dict:
    out = {}
    for table in sorted(tenancy.tables_of(tenancy.OWNED)):
        n = pg.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_id=?", (owner,)).fetchone()[0]
        if n:
            out[table] = n
    return out


def _sees(db, uid, call) -> bool:
    with _as(db, uid):
        return db.execute("SELECT COUNT(*) FROM calls WHERE node_id=?", (call,)).fetchone()[0] == 1


def test_reassign_moves_the_work_and_deletes_what_describes_the_person(org, pg_owner):
    db = org
    call = _work(db, A)
    _work(db, B)
    b_before = _owned(pg_owner, B)
    a_before = _owned(pg_owner, A)
    c_before = _owned(pg_owner, C)
    assert set(a_before) == set(tenancy.tables_of(tenancy.OWNED))       # the fixture covers every OWNED table
    assert _sees(db, M, call) and not _sees(db, N, call)
    with _as(db, D):
        result = offboard.offboard(db, A, "reassign", C)
    assert _owned(pg_owner, A) == {}
    after = _owned(pg_owner, C)
    for table in tenancy.tables_of(tenancy.OWNED):
        moved = after.get(table, 0) - c_before.get(table, 0)
        if table in tenancy.PERSONAL:
            assert moved == 0, table
        elif table == "comments":
            assert moved == a_before[table] - 1, table                  # the coaching note about A is gone
        else:
            assert moved == a_before[table], table
    note = pg_owner.execute("SELECT owner_id, author_id FROM comments WHERE entity_id=? AND entity_type='call' "
                            "AND body='Ask for the CFO'", (call,)).fetchone()
    assert tuple(note) == (C, M)                                           # moved with the call, author kept
    assert pg_owner.execute("SELECT COUNT(*) FROM comments WHERE entity_type='coaching' AND entity_id=?",
                            (A,)).fetchone()[0] == 0
    assert pg_owner.execute("SELECT owner_user_id FROM access_log WHERE entity_id=?", (call,)).fetchone()[0] == C
    for table in ("user_state", "user_speaker_labels"):
        assert pg_owner.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (A,)).fetchone()[0] == 0
    # visibility follows the owner: C's manager reads the moved call, A's manager (not C's) no longer does
    assert _sees(db, N, call) and _sees(db, C, call) and not _sees(db, M, call)
    # B's work is untouched
    assert _owned(pg_owner, B) == b_before
    # A is disabled and signed out; the audit says what happened
    row = pg_owner.execute("SELECT status FROM users WHERE id=?", (A,)).fetchone()
    assert row[0] == "disabled" and result["sessions_revoked"] == 1
    assert pg_owner.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND revoked_at IS NULL", (A,)).fetchone()[0] == 0
    audit = pg_owner.execute("SELECT kind, owner_id, actor_user_id, after FROM events WHERE kind LIKE 'admin.user.%' "
                             "ORDER BY id").fetchall()
    kinds = [r["kind"] for r in audit]
    assert kinds == ["admin.user.offboard", "admin.user.disable"]
    first = json.loads(audit[0]["after"])
    assert audit[0]["owner_id"] == D and audit[0]["actor_user_id"] == D
    assert first["mode"] == "reassign" and first["to_user_id"] == C and first["counts"]["calls"] == a_before["calls"]


def test_purge_deletes_every_owned_row_and_keeps_the_directory(org, pg_owner):
    db = org
    _work(db, A)
    _work(db, B)
    people = pg_owner.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    accounts = pg_owner.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    b_before = _owned(pg_owner, B)
    with _as(db, D):
        result = offboard.offboard(db, A, "purge")
    assert _owned(pg_owner, A) == {}
    assert _owned(pg_owner, B) == b_before
    assert result["counts"]["calls"] >= 1 and result["counts"]["nodes"] >= 1
    assert pg_owner.execute("SELECT COUNT(*) FROM people").fetchone()[0] == people
    assert pg_owner.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == accounts
    assert pg_owner.execute("SELECT COUNT(*) FROM access_log WHERE owner_user_id=?", (A,)).fetchone()[0] == 0
    assert pg_owner.execute("SELECT status FROM users WHERE id=?", (A,)).fetchone()[0] == "disabled"
    after = json.loads(pg_owner.execute("SELECT after FROM events WHERE kind='admin.user.offboard'").fetchone()[0])
    assert after["mode"] == "purge" and after["to_user_id"] is None


def test_refusals(org, pg_owner):
    db = org
    _work(db, A)
    for who in (A, M):                                           # nobody but an admin reaches the function
        with _as(db, who):
            with pytest.raises(Exception, match="only an active admin"):
                db.execute("SELECT app_offboard(?, NULL)", (B,))
            db.rollback()
    with _as(db, D, mode=identity.SERVICE):                      # nor an admin's background session
        with pytest.raises(Exception, match="only an active admin"):
            db.execute("SELECT app_offboard(?, NULL)", (B,))
        db.rollback()
    with _as(db, D):
        with pytest.raises(offboard.OffboardError, match="yourself"):
            offboard.offboard(db, D, "purge")
        with pytest.raises(offboard.OffboardError, match="not an active rep"):
            offboard.offboard(db, A, "reassign", M)
        users.update(db, B, status="disabled")
        db.commit()
        with pytest.raises(offboard.OffboardError, match="not an active rep"):
            offboard.offboard(db, A, "reassign", B)
        with pytest.raises(offboard.OffboardError, match="choose who"):
            offboard.offboard(db, A, "reassign", None)
    assert _owned(pg_owner, A).get("calls") == 2                  # nothing happened (the factory made two)


def test_the_admin_page_offboards_after_the_email_is_typed(org, pg_owner):
    db = org
    call = _work(db, A)
    seed_org_settings(pg_owner)
    app = app_module.create_app(start_worker=False, live_factory=None, hub=None)
    client = TestClient(app, follow_redirects=False)
    head = {"x-test-user": D, "accept": "text/html", **ORIGIN}
    page = client.get("/admin", headers=head).text
    assert f'action="/admin/users/{A}/offboard"' in page and "Hand their work to" in page
    r = client.post(f"/admin/users/{A}/offboard", data={"mode": "reassign", "to_user_id": C, "confirm": "nope"},
                    headers=head)
    assert r.status_code == 303 and "confirm" in r.headers["location"]
    assert _owned(pg_owner, A).get("calls") == 2
    r = client.post(f"/admin/users/{A}/offboard", data={"mode": "reassign", "to_user_id": C, "confirm": f"{A}@tessel.test"},
                    headers=head)
    assert r.status_code == 303 and "Offboarded" in r.headers["location"].replace("+", " ")
    assert pg_owner.execute("SELECT owner_id FROM calls WHERE node_id=?", (call,)).fetchone()[0] == C
    # a rep cannot reach the route at all
    r = client.post(f"/admin/users/{B}/offboard", data={"mode": "purge", "confirm": f"{B}@tessel.test"},
                    headers={**head, "x-test-user": C})
    assert r.status_code == 404
