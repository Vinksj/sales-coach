"""intel/coach.build() names the coached seller in every read (defence in depth).

build() read seller_observations, the per-call analyses and the live nudges with no owner filter: correct only
while it runs in a service session, where the row-level policies show a user their own rows. A manager's
INTERACTIVE session also reads the team's rows (store/rls.py app_visible_owners), and on SQLite nothing filters
at all (but a SQLite store holds one user, whose rows are all 'local': the case is Postgres's).
"""
import pytest

from salescoach import identity, users
from salescoach.intel import coach

from test_p3_coach_prep_embed import _analysed_call, _nudge
from test_p3_strategy import intel_env  # noqa: F401  (autouse fixture)

pytestmark = pytest.mark.postgres_only

A, M = "u-a", "u-m"


def test_a_managers_interactive_build_sees_only_their_own_rows(db):
    users.create_team(db, "West", team_id="t-west")
    users.create(db, "a@tessel.test", "Asha", role="rep", team_id="t-west", user_id=A)
    users.create(db, "m@tessel.test", "Mani", role="manager", user_id=M)
    users.set_managers(db, "t-west", [M])
    db.commit()
    with identity.as_actor(db, identity.Actor(A, role="rep")):
        rep_calls = [_analysed_call(db, day, ["accepts_vague_commitments"]) for day in (1, 2)]
        _nudge(db, rep_calls[0], "ignored")
        db.commit()
    with identity.as_actor(db, identity.Actor(M, role="manager")):
        own = _analysed_call(db, 3, ["secures_specific_next_step"])
        _nudge(db, own, "followed")
        db.commit()
        ctx = coach.build(db)                          # interactive: the policies would let M read A's rows
    assert [c["node_id"] for c in ctx["calls"]] == [own]
    assert {o["call_id"] for o in ctx["observations"]} == {own}
    assert {i["call_id"] for i in ctx["insights"]} == {own}
    assert [n["call_id"] for n in ctx["nudges"]["rows"]] == [own]
    assert not any(cid in coach.CoachAgent().build_prompt(ctx) for cid in rep_calls)
