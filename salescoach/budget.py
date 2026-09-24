"""Daily model-spend caps (Phase 6).

Two optional caps, in USD per day, checked by agents/base.Agent.run BEFORE a provider is called:
  LLM_BUDGET_USER_USD_DAY   the acting owner's spend today
  LLM_BUDGET_ORG_USD_DAY    everyone's spend today
Each comes from the environment first, else from the `budget` settings (config/budget.yaml, which the
org_settings overlay replaces in cloud mode): {llm: {user_usd_day: 5, org_usd_day: 50}}. Unset or 0 =
no cap. "Today" is the acting owner's calendar day (seller.zone()), applied to both sums, so a cap
resets at the rep's midnight, not at UTC's.

Spend is SUM(agent_runs.cost_usd) over runs started today. cost_usd is what the provider reported
(providers/pricing.py for the HTTP providers, the CLI's own figure for claude_code); an unpriced
model contributes nothing, so a cap only bites on what can be counted.

When a cap is reached the agent records a run row (status error, error "budget_deferred: ...") and
raises AgentFailed(rate_limited=True): the worker defers the event without spending an attempt
(orchestrator/worker._settle_failure), exactly as for a provider's 429, and the event is claimed
again after RATE_LIMIT_DEFER_S, by when the day may have turned.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config, identity, seller

USER_ENV = "LLM_BUDGET_USER_USD_DAY"
ORG_ENV = "LLM_BUDGET_ORG_USD_DAY"


class BudgetExceeded(RuntimeError):
    def __init__(self, scope: str, cap: float, spent: float):
        self.scope, self.cap, self.spent = scope, cap, spent
        super().__init__(f"the {scope} model budget for today is used up: {spent:.2f} of {cap:.2f} USD")


def _positive(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def caps() -> dict:
    """{"user": USD|None, "org": USD|None}: the environment, else the budget settings."""
    settings = (config.load("budget") or {}).get("llm") or {}
    return {"user": _positive(os.environ.get(USER_ENV)) or _positive(settings.get("user_usd_day")),
            "org": _positive(os.environ.get(ORG_ENV)) or _positive(settings.get("org_usd_day"))}


def day_start_utc(zone=None) -> str:
    """The start of today in `zone` (the acting owner's, by default), as the UTC ISO text agent_runs
    compares with (store.engine.now() writes '+00:00' timestamps at second precision)."""
    zone = zone or seller.zone()
    local_midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def day_end_utc(zone=None) -> str:
    zone = zone or seller.zone()
    local_midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return (local_midnight + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")


def spent_today(conn, owner_id: Optional[str] = None, since: Optional[str] = None) -> float:
    """SUM(cost_usd) of runs started today by `owner_id` (None = everyone)."""
    since = since or day_start_utc()
    if owner_id is None:
        row = conn.execute("SELECT SUM(cost_usd) FROM agent_runs WHERE started_at >= ?", (since,)).fetchone()
    else:
        row = conn.execute("SELECT SUM(cost_usd) FROM agent_runs WHERE owner_id=? AND started_at >= ?",
                           (owner_id, since)).fetchone()
    return float(row[0] or 0.0)


def check(conn, owner_id: Optional[str] = None) -> None:
    """Raise BudgetExceeded when the owner's or the org's spend today has reached its cap. Cheap when no
    cap is set (no query)."""
    limits = caps()
    if not limits["user"] and not limits["org"]:
        return
    owner_id = owner_id or identity.actor_of(conn).user_id
    since = day_start_utc()
    if limits["user"]:
        mine = spent_today(conn, owner_id, since)
        if mine >= limits["user"]:
            raise BudgetExceeded("user", limits["user"], mine)
    if limits["org"]:
        everyone = spent_today(conn, None, since)
        if everyone >= limits["org"]:
            raise BudgetExceeded("org", limits["org"], everyone)


def usage_today(conn, mine_only: bool = False) -> dict:
    """The Usage panel: today's spend for the org and per user (or the acting user alone), with the
    caps, the unpriced-run count and the day window. Numbers from SQL, nothing estimated."""
    since, until = day_start_utc(), day_end_utc()
    limits = caps()
    me = identity.actor_of(conn).user_id
    rows = conn.execute(
        "SELECT owner_id, SUM(cost_usd) AS spent, COUNT(*) AS runs, "
        "SUM(CASE WHEN cost_usd IS NULL AND status='ok' THEN 1 ELSE 0 END) AS unpriced, "
        "SUM(CASE WHEN error LIKE 'budget_deferred:%' THEN 1 ELSE 0 END) AS deferred "
        "FROM agent_runs WHERE started_at >= ? GROUP BY owner_id ORDER BY owner_id", (since,)).fetchall()
    per_user = [{"user_id": r["owner_id"], "spent": float(r["spent"] or 0.0), "runs": int(r["runs"] or 0),
                 "unpriced": int(r["unpriced"] or 0), "deferred": int(r["deferred"] or 0)} for r in rows]
    names = {}
    if per_user:
        from . import users
        for u in users.list_users(conn):
            names[u["id"]] = u["name"] or u["email"] or u["id"]
    for entry in per_user:
        entry["name"] = names.get(entry["user_id"], entry["user_id"])
    org_total = sum(e["spent"] for e in per_user)
    mine = next((e for e in per_user if e["user_id"] == me), None) or {"user_id": me, "spent": 0.0, "runs": 0,
                                                                        "unpriced": 0, "deferred": 0}
    return {"since": since, "until": until, "tz": seller.tz_label(), "caps": limits, "org": org_total,
            "mine": mine, "users": [] if mine_only else per_user,
            "unpriced": sum(e["unpriced"] for e in per_user)}
