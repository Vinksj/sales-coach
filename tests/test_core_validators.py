from salescoach.validators import evidence, recipients, voice_lint

TURNS = {
    0: {"text": "I'll send you the plant-wise breakdown by Friday.", "channel": "me", "quality": "ok", "bleed_flag": 0},
    1: {"text": "Okay. And I'll try to set up a meeting with our CFO.", "channel": "them", "quality": "ok", "bleed_flag": 0},
    2: {"text": "Hi chloral cara march salrat weather.", "channel": "them", "quality": "garbled", "bleed_flag": 0},
    3: {"text": "We will close by March.", "channel": "me", "quality": "ok", "bleed_flag": 1},
}


def test_exact_quote_keeps_confidence():
    conf, notes, check = evidence.judge("explicit", "send you the plant-wise breakdown", [0], TURNS, owner="me")
    assert conf == "explicit" and check.how == "exact" and not notes


def test_fabricated_quote_drops_to_low():
    conf, notes, check = evidence.judge("explicit", "we have budget approved for this", [1], TURNS)
    assert conf == "low" and not check.found and "quote not found" in notes[0]


def test_unknown_turn_and_no_turns():
    assert evidence.check("x", [99], TURNS).how == "unknown_turn"
    assert evidence.check("x", [], TURNS).how == "no_turns"


def test_garbled_and_bleed_cap_at_medium():
    conf, notes, _ = evidence.judge("explicit", "chloral cara march", [2], TURNS)
    assert conf == "medium" and any("garbled" in n for n in notes)
    conf, notes, _ = evidence.judge("high", "we will close by march", [3], TURNS)
    assert conf == "medium" and any("leaking" in n for n in notes)


def test_owner_channel_mismatch_caps():
    conf, notes, _ = evidence.judge("explicit", "try to set up a meeting with our CFO", [1], TURNS, owner="me")
    assert conf == "medium" and any("owner is me" in n for n in notes)


def test_quote_across_neighbouring_turn_and_fuzzy():
    assert evidence.check("by Friday. Okay. And I'll try", [0], TURNS).found
    assert evidence.check("I'll try to set up the meeting with our CFO", [1], TURNS).how == "fuzzy"


def test_recommended_items_need_no_quote():
    conf, _, _ = evidence.judge("medium", "", [], TURNS, requires_quote=False)
    assert conf == "medium"


def test_recipients():
    allowed = {"arjun@nwp.in": "Arjun"}
    assert recipients.check(["arjun@nwp.in"], [], allowed) == []
    assert "not on this call or deal" in recipients.check(["cfo@nwp.in"], [], allowed)[0]
    assert recipients.check(["cfo@nwp.in"], [], allowed, user_added=["cfo@nwp.in"]) == []
    assert recipients.check([], [], allowed) == ["no recipient in To"]
    assert any("not an email" in v for v in recipients.check(["arjun"], [], allowed))


def test_voice_lint_blocks_and_warns():
    issues = voice_lint.lint("NWP next steps", "Let's meet on [SLOTS]. Let me know your availability. We leverage AI.")
    kinds = {(i.kind, i.severity) for i in issues}
    assert ("slots", "block") in kinds and ("scheduling_burden", "block") in kinds
    assert ("banned_word", "warn") in kinds
    assert voice_lint.blocking(voice_lint.lint("Subject", "Plain words. Thanks.")) == []


def test_autofix_em_dash():
    assert "—" not in voice_lint.autofix("The pilot — at Pant Nagar — starts Monday")
    assert voice_lint.autofix("floor--ceiling") == "floor, ceiling"
