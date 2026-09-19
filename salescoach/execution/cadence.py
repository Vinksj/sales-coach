"""When to next look at an open loop, from config/cadence.yaml.

No generic "three days later": the first rule matching the loop's type,
priority and due-date confidence wins, counted from its due date when it has
one, else from the call date.
"""
from datetime import date, timedelta

from .. import config


def _matches(rule_when: dict, loop: dict) -> bool:
    return all(loop.get(k) == v for k, v in (rule_when or {}).items())


def add_business_days(start: date, days: int) -> date:
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def next_check(loop: dict, call_date: date) -> date:
    cfg = config.load("cadence")
    rules = cfg.get("rules") or [{"when": {}, "check_after_days": 5}]
    rule = next((r for r in rules if _matches(r.get("when"), loop)), rules[-1])
    ref = date.fromisoformat(loop["due_date"]) if loop.get("due_date") else call_date
    days = int(rule.get("check_after_days", 5))
    if cfg.get("business_days_only", True):
        return add_business_days(ref, days)
    return ref + timedelta(days=days)
