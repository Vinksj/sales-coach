"""The budget day is the org's, not the acting rep's (Postgres, cloud mode).

The daily caps (user AND org) counted from midnight in the ACTING rep's profile timezone, which the rep edits
on /me/setup: a capped rep moved their timezone to one whose day had just begun and both sums restarted.
Now one zone serves everyone (budget.budget_zone(): the budget settings' llm.timezone, default UTC).
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from salescoach import budget, config, identity

from test_route_crawl import A, ORIGIN, client, cloud, objects, two_reps  # noqa: F401

pytestmark = pytest.mark.postgres_only


def _zone(pred):
    for off in range(-12, 15):
        name = f"Etc/GMT{'+' if off <= 0 else '-'}{abs(off)}" if off else "Etc/GMT"
        if pred(datetime.now(ZoneInfo(name)).hour):
            return name
    raise AssertionError("no zone")


def test_moving_ones_own_timezone_does_not_reset_either_cap(client, db, pg_owner, monkeypatch):
    monkeypatch.setenv(budget.USER_ENV, "5")
    monkeypatch.setenv(budget.ORG_ENV, "5")
    fresh = _zone(lambda h: h == 0)                                      # a day less than an hour old
    save = lambda tz: client.post("/me/setup", data={"name": "Asha Rao", "emails": f"{A}@tessel.test",  # noqa: E731
                                                     "timezone": tz}, headers={"x-test-user": A, "accept": "text/html", **ORIGIN})
    ran = datetime.now(timezone.utc).replace(microsecond=0)              # a run earlier in today's UTC day:
    utc_midnight = ran.replace(hour=0, minute=0, second=0)               # before the fresh zone's midnight when
    ran = max(utc_midnight, ran - timedelta(minutes=90)).isoformat(timespec="seconds")   # there is room for it
    call = pg_owner.execute("SELECT node_id FROM calls WHERE owner_id=? LIMIT 1", (A,)).fetchone()[0]
    pg_owner.execute("INSERT INTO agent_runs(call_id,agent,started_at,status,cost_usd,owner_id) "
                     "VALUES (?,'call_analyst',?,'ok',12.0,?)", (call, ran, A))
    pg_owner.commit()
    with identity.as_user(db, A, mode=identity.SERVICE):
        with pytest.raises(budget.BudgetExceeded):
            budget.check(db)
    assert save(fresh).status_code == 303
    with identity.as_user(db, A, mode=identity.SERVICE):
        with pytest.raises(budget.BudgetExceeded):                       # still capped: the day is the org's
            budget.check(db)
        assert budget.spent_today(db, A) == 12.0 and budget.spent_today(db, None) == 12.0


def test_the_org_setting_names_the_zone(db, two_reps, cloud):
    with identity.as_user(db, A):
        assert budget.budget_zone() == ZoneInfo("UTC")
        assert budget.day_start_utc().endswith("T00:00:00+00:00")
    db.execute("INSERT INTO org_settings(name,body,version) VALUES ('budget',?,1)",
               ('{"llm": {"timezone": "Asia/Kolkata"}}',))
    db.commit()
    config._org_cache.clear()
    with identity.as_user(db, A):
        assert budget.budget_zone() == ZoneInfo("Asia/Kolkata")
        assert budget.day_start_utc().endswith("T18:30:00+00:00")
