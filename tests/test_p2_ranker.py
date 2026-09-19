"""Phase 2 ranker: the over-coaching guard. One visible nudge, cooldown,
first-minute rule, moment staleness, repeat rule, per-call cap, minimum
score, resolution, and a reason on everything held back."""
import pytest

from salescoach.coach import settings
from salescoach.coach.detectors import make_candidate
from salescoach.coach.ranker import Ranker
from salescoach.coach.state import ConversationState, Seg
from salescoach.coach.text import Vocab


@pytest.fixture
def setup():
    def build(**budget):
        cfg = settings.load({"budget": budget} if budget else None)
        state = ConversationState(cfg, Vocab(cfg))
        state.set_phase("discovery", "slow", 0)
        state.cfg["phase"]["slow_hold_s"] = 10 ** 9          # keep the phase fixed for scoring
        return cfg, state, Ranker(cfg)
    return build


def cand(cfg, state, trigger, t, conf=0.8, **kw):
    seg = Seg(int(t), "them", t - 3, t, f"line at {t}")
    return make_candidate(cfg, trigger, conf, t, seg, state, **kw)


def test_first_minute_only_objection(setup):
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "buying_signal", 30))
    r.offer(cand(cfg, state, "objection", 31))
    winner, _ = r.select(31, state)
    assert winner.trigger == "objection"
    assert r.pending["buying_signal"].last_block in ("warmup", "visible")


def test_warmup_blocks_then_lifts_for_state_triggers(setup):
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "stakeholder_gap", 40))
    assert r.select(40, state)[0] is None and r.pending["stakeholder_gap"].last_block == "warmup"
    assert r.select(61, state)[0].trigger == "stakeholder_gap"


def test_one_visible_then_cooldown_then_stale(setup):
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "objection", 100))
    first, _ = r.select(100, state)
    assert first.shown and first.shown_at == 100
    r.offer(cand(cfg, state, "weak_commitment", 105))
    assert r.select(105, state)[0] is None
    assert r.pending["weak_commitment"].last_block == "visible"
    assert r.select(115, state)[0] is None                       # display over (12 s), cooldown still on
    assert r.pending["weak_commitment"].last_block == "cooldown"
    _, retired = r.select(131, state)                            # moment triggers are stale after 25 s
    assert [c.suppressed_reason for c in retired] == ["cooldown"]
    assert len(r.shown) == 1


def test_state_trigger_survives_cooldown(setup):
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "objection", 100))
    r.select(100, state)
    r.offer(cand(cfg, state, "stakeholder_gap", 120))
    assert r.select(150, state)[0] is None
    winner, _ = r.select(191, state)
    assert winner is not None and winner.trigger == "stakeholder_gap"


def test_moment_goes_stale_without_any_block(setup):
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "buying_signal", 100))
    _, retired = r.select(130, state)
    assert retired[0].suppressed_reason == "stale"


def test_repeat_needs_a_clearly_stronger_score(setup):
    cfg, state, r = setup()
    state.set_phase("close", "slow", 0)                          # weak_commitment's phase fit is 1.0 at the close
    r.offer(cand(cfg, state, "weak_commitment", 100, conf=0.5))
    first, _ = r.select(100, state)
    r.offer(cand(cfg, state, "weak_commitment", 200, conf=0.55))
    assert r.select(200, state)[0] is None and r.pending["weak_commitment"].last_block == "repeat"
    r.offer(cand(cfg, state, "weak_commitment", 201, conf=0.95))
    winner, retired = r.select(201, state)
    assert winner is not None and winner.confidence == 0.95
    assert retired == [] or all(c.suppressed_reason for c in retired)


def test_repeat_margin_ignores_the_novelty_discount(setup):
    # 0.9 is 29% above 0.7: clearly stronger. Novelty (x0.7) must not count against it twice.
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "objection", 100, conf=0.7, entity="price"))
    r.select(100, state)
    r.offer(cand(cfg, state, "objection", 200, conf=0.9, entity="incumbent"))
    winner, _ = r.select(200, state)
    assert winner is not None and winner.entity == "incumbent" and winner.score == pytest.approx(0.63)
    r.offer(cand(cfg, state, "objection", 400, conf=0.8, entity="timing"))
    assert r.select(400, state)[0] is None and r.pending["objection"].last_block == "repeat"


def test_blocked_candidate_waits_and_keeps_its_block_as_reason(setup):
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "objection", 100))
    r.select(100, state)
    r.offer(cand(cfg, state, "weak_commitment", 105))            # fresh score 0.43, decays under 0.35 by 115
    r.select(105, state)
    assert r.select(120, state)[0] is None and r.pending["weak_commitment"].score < r.min_score
    assert [c.suppressed_reason for c in r.flush()] == ["cooldown"]


def test_per_call_cap(setup):
    cfg, state, r = setup(max_per_call=2, cooldown_s=0, display_s=0)
    for i, trigger in enumerate(["objection", "buying_signal", "weak_commitment"]):
        r.offer(cand(cfg, state, trigger, 100 + i * 10))
        winner, retired = r.select(100 + i * 10, state)
    assert len(r.shown) == 2 and winner is None and retired[0].suppressed_reason == "max_per_call"


def test_min_score(setup):
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "status_quo", 100, conf=0.3))
    winner, retired = r.select(100, state)
    assert winner is None and retired[0].suppressed_reason == "below_min_score"


def test_outranked_and_duplicate(setup):
    cfg, state, r = setup()
    retired = r.offer(cand(cfg, state, "buying_signal", 100, conf=0.55))
    retired += r.offer(cand(cfg, state, "buying_signal", 100, conf=0.8))
    assert [c.suppressed_reason for c in retired] == ["duplicate"]
    r.offer(cand(cfg, state, "objection", 100, conf=0.9))
    winner, _ = r.select(100, state)
    assert winner.trigger == "objection" and r.pending["buying_signal"].last_block == "outranked"


def test_resolved_when_me_asks(setup):
    cfg, state, r = setup()
    state.add(Seg(1, "them", 90, 95, "Detention is a big problem for our trucks."))
    c = cand(cfg, state, "quantify_impact", 96)
    r.offer(c)
    r.offer(cand(cfg, state, "objection", 96, conf=0.9))
    r.select(96, state)                                          # objection wins; quantify waits
    state.add(Seg(2, "me", 97, 100, "How much does that cost you each month?"))
    _, retired = r.select(101, state)
    assert [x.suppressed_reason for x in retired] == ["resolved"]


def test_dismiss_clears_visible_but_not_cooldown(setup):
    cfg, state, r = setup()
    r.offer(cand(cfg, state, "objection", 100))
    shown, _ = r.select(100, state)
    shown.id = 7
    assert r.dismiss(7) and r.current(101) is None
    r.offer(cand(cfg, state, "buying_signal", 102))
    assert r.select(102, state)[0] is None and r.pending["buying_signal"].last_block == "cooldown"


def test_too_long_is_refused(setup):
    cfg, state, r = setup()
    c = cand(cfg, state, "objection", 100, text="one two three four five six seven eight nine ten eleven twelve "
                                              "thirteen fourteen fifteen sixteen")
    assert r.offer(c)[0].suppressed_reason == "too_long"
