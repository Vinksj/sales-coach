"""What a model call cost, from the usage counts the API returned and the price table in
config/models.yaml:

    prices:
      claude-opus-5: {price_per_mtok_in: 5.00, price_per_mtok_out: 25.00}

A model id matches its own key exactly, or a key that is a dash-separated prefix of it
(`claude-sonnet-5` prices `claude-sonnet-5-20260401`; it does not price `claude-sonnet-50`). A model
with no entry costs None, never a guess: the budgets (salescoach/budget.py) then cannot see it, which
is the honest answer and the reason to keep the table current (Setup lists the models an account has).
Cache reads/writes and other usage fields are not priced.
"""
from typing import Optional

from .. import config


def price_for(model: str) -> Optional[tuple[float, float]]:
    """(USD per million input tokens, USD per million output tokens) for `model`, else None."""
    model = (model or "").strip()
    if not model:
        return None
    table = config.load("models").get("prices") or {}
    if not isinstance(table, dict):
        return None
    best, best_len = None, -1
    for key, entry in table.items():
        key = str(key).strip()
        if not isinstance(entry, dict) or not key:
            continue
        if model == key or (model.startswith(key + "-") and len(key) > best_len):
            if model == key:
                best, best_len = entry, len(model) + 1     # an exact match beats every prefix
            elif best_len < len(model) + 1:
                best, best_len = entry, len(key)
    if best is None:
        return None
    try:
        return float(best["price_per_mtok_in"]), float(best["price_per_mtok_out"])
    except (KeyError, TypeError, ValueError):
        return None


def cost_usd(model: str, input_tokens, output_tokens) -> Optional[float]:
    """The call's cost, or None when the model is unpriced or the counts are missing."""
    prices = price_for(model)
    if prices is None or input_tokens is None or output_tokens is None:
        return None
    try:
        tokens_in, tokens_out = int(input_tokens), int(output_tokens)
    except (TypeError, ValueError):
        return None
    if tokens_in < 0 or tokens_out < 0:
        return None
    return round((tokens_in * prices[0] + tokens_out * prices[1]) / 1_000_000, 6)
