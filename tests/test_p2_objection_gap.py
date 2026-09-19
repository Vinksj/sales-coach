"""An objection may follow an earlier nudge after a short gap instead of the full cooldown.

From the real replay of the 2 Sep Northwind call: a price objection 46 s after a
weaker buying-signal nudge was lost to the 90 s cooldown. One nudge on screen at
a time still holds, and every other trigger still waits out the cooldown.
"""
import pytest

from salescoach.coach import settings
from salescoach.coach.detectors import make_candidate
from salescoach.coach.ranker import Ranker
from salescoach.coach.state import ConversationState, Seg
from salescoach.coach.text import Vocab


@pytest.fixture
def setup():
    cfg = settings.load(None)
    state = ConversationState(cfg, Vocab(cfg))
    state.set_phase("discovery", "slow", 0)
    state.cfg["phase"]["slow_hold_s"] = 10 ** 9
    return cfg, state, Ranker(cfg)


def cand(cfg, state, trigger, t, conf=0.9):
    seg = Seg(int(t), "them", t - 3, t, f"line at {t}")
    return make_candidate(cfg, trigger, conf, t, seg, state)


def test_gap_is_configured_shorter_than_the_cooldown(setup):
    cfg, _, r = setup
    assert r.objection_min_gap_s == 20 and r.cooldown_s == 90


def test_objection_waits_the_short_gap_not_the_cooldown(setup):
    cfg, state, r = setup
    r.offer(cand(cfg, state, "buying_signal", 100))
    assert r.select(100, state)[0].trigger == "buying_signal"
    r.offer(cand(cfg, state, "objection", 108))
    assert r.select(108, state)[0] is None
    assert r.pending["objection"].last_block == "visible"        # the first nudge is still on screen
    assert r.select(115, state)[0] is None
    assert r.pending["objection"].last_block == "cooldown"       # off screen, but inside the 20 s gap
    winner, _ = r.select(121, state)
    assert winner is not None and winner.trigger == "objection"


def test_other_triggers_still_wait_the_full_cooldown(setup):
    cfg, state, r = setup
    r.offer(cand(cfg, state, "objection", 100))
    assert r.select(100, state)[0].trigger == "objection"
    r.offer(cand(cfg, state, "weak_commitment", 121))
    assert r.select(121, state)[0] is None
    assert r.pending["weak_commitment"].last_block == "cooldown"
