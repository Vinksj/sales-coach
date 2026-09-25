"""Security review, finding 1: a manager demoted to rep (or disabled, or offboarded) reads no team's work.

Two layers, each tested on its own: adminui/ops.py deletes the user's team_managers rows when the role becomes
rep and when they are disabled or offboarded; and app_visible_owners() (the policies) and
access.managed_team_ids() (the Team page and nav) both require the role to be manager or admin, so a
team_managers row left behind by any other path grants nothing. Postgres only (cloud mode).
"""
import pytest

from salescoach import identity
from salescoach.adminui import ops
from salescoach.manager import access
from test_route_crawl import cloud  # noqa: F401
from test_manager_review import A, D, M, ORIGIN, _as, client, get, org, post, work  # noqa: F401

pytestmark = pytest.mark.postgres_only


def _seats(pg_owner, uid):
    return pg_owner.execute("SELECT COUNT(*) FROM team_managers WHERE user_id=?", (uid,)).fetchone()[0]


def test_demoting_a_manager_to_rep_ends_their_team_access(client, work, pg_owner):
    call = work["a_call"]
    assert get(client, M, f"/calls/{call}").status_code == 200           # the manager reads A's call
    r = post(client, D, f"/admin/users/{M}", {"role": "rep", "team_id": ""})
    assert r.status_code == 303, r.text
    assert pg_owner.execute("SELECT role FROM users WHERE id=?", (M,)).fetchone()[0] == "rep"
    assert _seats(pg_owner, M) == 0                                       # the seat went with the role
    page = get(client, M, f"/calls/{call}")
    assert page.status_code in (403, 404) and "We need a pilot" not in page.text
    assert get(client, M, "/team").status_code != 200
    event = pg_owner.execute("SELECT after FROM events WHERE kind='admin.user.update' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "managed_teams_removed" in event and "t-west" in event          # audited: which team they managed


def test_a_leftover_team_managers_row_grants_a_rep_nothing(db, client, work, pg_owner):
    """The backstop: the role is demoted by a path that leaves team_managers alone (an operator at psql)."""
    pg_owner.execute("UPDATE users SET role='rep' WHERE id=?", (M,))
    pg_owner.commit()
    assert _seats(pg_owner, M) == 1
    page = get(client, M, f"/calls/{work['a_call']}")
    assert page.status_code in (403, 404) and "We need a pilot" not in page.text
    with _as(db, M):
        assert db.execute("SELECT app_visible_owners()").fetchone()[0] == [M]
        assert access.managed_team_ids(db) == [] and not access.manages_team(db)
        assert db.execute("SELECT COUNT(*) FROM calls WHERE owner_id=?", (A,)).fetchone()[0] == 0
    pg_owner.execute("UPDATE users SET role='manager' WHERE id=?", (M,))   # promoted again: the row counts again
    pg_owner.commit()
    with _as(db, M):
        assert access.managed_team_ids(db) == ["t-west"]
        assert db.execute("SELECT COUNT(*) FROM calls WHERE owner_id=?", (A,)).fetchone()[0] >= 1


def test_disabling_a_manager_removes_their_seats(db, org, pg_owner):
    with identity.as_actor(db, identity.Actor(D, role="admin")):
        ops.disable(db, M)
        db.commit()
        ops.enable(db, M)                                                 # enabling does not bring the seat back
        db.commit()
    assert _seats(pg_owner, M) == 0
    kinds = [r[0] for r in pg_owner.execute("SELECT after FROM events WHERE kind='admin.user.disable'").fetchall()]
    assert kinds and "t-west" in kinds[-1]
