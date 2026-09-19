"""Live-coach settings: config/live_coach.yaml, optionally overridden per run.

Layers, weakest first: the tracked file, the active sales methodology's `coaching.live_weights`
(a team that sells with SPIN wants the "quantify the impact" nudge to win more often), the learned
boost (phase F2: the trigger behind the seller's top learned weakness, a small bounded step), the
user's own live_coach.yaml (an accepted learning proposal, a hand edit), the overrides of this run.
So an explicit user setting always beats what the coach learned by itself.

Overrides are merged key by key so a test or a replay can change one
threshold (budget.cooldown_s) without restating the whole file. The cached
YAML is deep-copied first: config.load() returns a shared dict, and a run
that mutated it would silently retune every later run in the process.
"""
import copy

from .. import config


def merge(base: dict, over: dict) -> dict:
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


WEIGHT_CEILING = 1.0


def load(overrides: dict | None = None, learned: dict | None = None) -> dict:
    """`learned` is {trigger: boost} from learning.feedback.live_focus; unknown triggers are ignored."""
    cfg = copy.deepcopy(config.load("live_coach"))
    if not cfg:
        raise RuntimeError("config/live_coach.yaml is missing or empty")
    weights = methodology_weights()
    if weights or learned:
        mine = ((config.load_user("live_coach").get("scoring") or {}).get("weights") or {})
        table = cfg.setdefault("scoring", {}).setdefault("weights", {})
        table.update(weights)
        for trigger, boost in (learned or {}).items():
            if trigger in table:
                table[trigger] = boosted(table[trigger], boost)
        table.update(mine)
    return merge(cfg, copy.deepcopy(overrides or {}))


def boosted(weight, boost) -> float:
    """weight + boost, never above 1.0 and never below the weight it started from."""
    weight = float(weight)
    return round(max(weight, min(WEIGHT_CEILING, weight + max(0.0, float(boost)))), 4)


def methodology_weights() -> dict:
    """Trigger weights the active methodology asks for. Unknown triggers are dropped, never added."""
    try:
        from ..intel import methodology
        wanted = methodology.active().live_weights
    except Exception:                      # the live coach must start whatever state the settings are in
        return {}
    known = (config.load("live_coach").get("scoring") or {}).get("weights") or {}
    return {k: float(v) for k, v in wanted.items() if k in known}
