"""Learning layer, the pattern store: counting, promotion at each boundary, dormancy and revival,
the user's verdicts, every family's rule, proposals that are never auto-applied, and for_prompt."""
import json
from datetime import date

import pytest

from salescoach import config
from salescoach.learning import observe, outcomes, patterns, voice
from salescoach.memory import gate
from salescoach.memory import patterns as seller_memory
from test_learning_support import (calls_with_tag, day, final_snapshot, live_nudge, make_call, make_deal, pattern,
                                   sent_edit, tag)
from test_p4_support import cfg, link_nudge, make_email, make_loop  # noqa: F401  (cfg is a fixture)

TAG = "avoids_budget"
PID = patterns.pattern_id("seller", TAG)


def _label(db, pid=PID):
    p = pattern(db, pid)
    return (p["status"], p["label"], p["n_calls"], p["n_deals"])


# ---- promotion boundaries -----------------------------------------------------------------------------

def test_two_calls_is_not_a_pattern_three_calls_over_two_deals_is_emerging(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, TAG, [(d1, 0, True), (d2, 1, True)])
    patterns.recompute(db)
    assert _label(db) == ("candidate", "", 2, 2)
    calls_with_tag(db, TAG, [(d1, 2, True)])
    patterns.recompute(db)
    assert _label(db) == ("active", "emerging", 3, 2)
    assert pattern(db, PID)["summary"] == "Avoiding the budget conversation"   # the taxonomy name, not a model's text


def test_three_calls_on_one_deal_is_not_emerging(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls = calls_with_tag(db, TAG, [(d1, 0, True), (d1, 1, True), (d1, 2, True), (d2, 3, False)])
    patterns.recompute(db)
    assert _label(db) == ("candidate", "", 3, 1)
    # Raw observations never count: three more rows on an already-counted call change nothing.
    for _ in range(3):
        tag(db, calls[0], TAG)
    patterns.recompute(db)
    assert _label(db) == ("candidate", "", 3, 1)


def test_thirty_percent_of_the_window_is_the_line(db, cfg):  # noqa: F811
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    # 3 calls over 2 deals, but only 2 of the last 7 analysed calls (28.6%): below the line.
    cfg["learning"] = {"promotion": {"window_calls": 7}}
    calls_with_tag(db, TAG, [(d1, 0, True)] + [(d2, n, n in (3, 6)) for n in range(1, 8)])
    patterns.recompute(db)
    assert _label(db) == ("candidate", "", 3, 2) and pattern(db, PID)["support"] == 2
    # The same history against a window of 10: 3 of 8 analysed calls, above the line.
    cfg["learning"] = {"promotion": {"window_calls": 10}}
    patterns.recompute(db)
    assert _label(db)[:2] == ("active", "emerging")
    # Exactly 30%: 3 of 10.
    calls_with_tag(db, TAG, [(d1, 8, False), (d1, 9, False)])
    patterns.recompute(db)
    p = pattern(db, PID)
    assert (p["label"], p["support"], json.loads(p["stats"])["window"]) == ("emerging", 3, 10)


def test_promote_function_at_each_boundary():
    window = [f"c{i}" for i in range(10)]                               # newest first
    assert patterns.promote(3, 2, {"c0", "c1", "c2"}, window) == "emerging"
    assert patterns.promote(2, 2, {"c0", "c1"}, window) == ""
    assert patterns.promote(3, 1, {"c0", "c1", "c2"}, window) == ""
    assert patterns.promote(3, 2, {"c0", "c1", "old"}, window) == ""    # 2 of 10 in the window
    six = {"c0", "c1", "c2", "c3", "c4", "x"}
    assert patterns.promote(6, 3, six, window) == "emerging"            # all in the newer half: not established
    assert patterns.promote(6, 3, {"c0", "c1", "c2", "c3", "c7", "x"}, window) == "established"
    assert patterns.promote(6, 2, {"c0", "c1", "c2", "c3", "c7", "x"}, window) == "emerging"   # 2 deals only
    assert patterns.promote(5, 3, {"c0", "c1", "c2", "c7", "x"}, window) == "emerging"         # 5 calls only


def test_established_needs_both_halves_of_the_window(db):
    deals = [make_deal(db, n) for n in "ABC"]
    # 10 analysed calls; the tag on the 6 NEWEST is 6 calls over 3 deals but only the newer half + one.
    plan = [(deals[n % 3], n, n >= 5) for n in range(10)]
    ids = calls_with_tag(db, TAG, plan)
    patterns.recompute(db)
    assert _label(db) == ("active", "emerging", 5, 3)
    tag(db, ids[1], TAG)                                                # now also in the older half, 6 calls
    patterns.recompute(db)
    assert _label(db) == ("active", "established", 6, 3)


def test_low_confidence_and_replays_are_not_counted(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, TAG, [(d1, 0, True), (d2, 1, True)])
    calls_with_tag(db, TAG, [(d1, 2, True)], confidence="low")
    patterns.recompute(db)
    assert _label(db) == ("candidate", "", 2, 2)
    assert db.execute("SELECT COUNT(*) FROM pattern_observations WHERE family='seller' AND key=?", (TAG,)).fetchone()[0] == 3

    call = make_call(db, d1, 3, source="capture")
    for _ in range(20):
        live_nudge(db, call, "dig_deeper", outcome="ignored", mode="replay")
    live_nudge(db, call, "dig_deeper", outcome="followed")
    live_nudge(db, call, "dig_deeper", outcome="ignored", shown=0)      # never on screen: not an observation
    patterns.recompute(db)
    p = pattern(db, patterns.pattern_id("nudge_trigger", "dig_deeper"))
    stats = json.loads(p["stats"])
    assert (stats["shown"], stats["followed"], stats["ignored"], stats["replays_not_counted"]) == (1, 1, 0, 20)
    assert patterns.open_proposals(db) == []                            # 20 replayed "ignored" propose nothing


# ---- dormancy, retirement, revival --------------------------------------------------------------------

def test_dormant_then_retired_then_returned(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, TAG, [(d1, 0, True), (d2, 1, True), (d1, 2, True)])
    patterns.recompute(db)
    assert _label(db)[:2] == ("active", "emerging")
    calls_with_tag(db, TAG, [(d2, n, False) for n in range(3, 13)])      # 10 analysed calls without it
    patterns.recompute(db)
    p = pattern(db, PID)
    assert (p["status"], p["label"], p["returned"]) == ("dormant", "", 0)
    assert patterns.for_prompt(db, "prep") == []

    calls_with_tag(db, TAG, [(d1, 13, True)])                            # it recurs: revived, flagged
    patterns.recompute(db)
    p = pattern(db, PID)
    assert (p["status"], p["returned"], p["n_calls"]) == ("candidate", 1, 4)   # 1 of the last 10: not emerging again yet

    calls_with_tag(db, TAG, [(d2, n, False) for n in range(14, 34)])      # 20 analysed calls without it
    patterns.recompute(db)
    assert pattern(db, PID)["status"] == "retired" and pattern(db, PID)["user_state"] is None
    calls_with_tag(db, TAG, [(d1, 40, True)])                             # retired by absence, not by the user: comes back
    patterns.recompute(db)
    assert pattern(db, PID)["status"] == "candidate" and pattern(db, PID)["returned"] == 1


def test_user_retired_never_revives_and_user_state_outranks_recompute(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, TAG, [(d1, 0, True)])
    patterns.recompute(db)
    assert _label(db)[0] == "candidate"
    patterns.set_user_state(db, PID, "confirmed")                        # one call, but the user says it is real
    assert _label(db)[0] == "active"
    prov = db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='user_state'", (PID,)).fetchone()
    assert prov["confidence"] == "user_input"
    patterns.recompute(db)
    assert _label(db)[0] == "active" and pattern(db, PID)["user_state"] == "confirmed"
    assert [p["id"] for p in patterns.for_prompt(db, "prep")] == [PID]
    # A machine write to the user's verdict is parked by the gate, never applied.
    assert gate.propose(db, gate.Proposed(PID, "learned_patterns", "user_state", None, "high", {"kind": "call"})) == "conflict"

    patterns.set_user_state(db, PID, "retired")
    calls_with_tag(db, TAG, [(d2, 1, True), (d1, 2, True), (d2, 3, True)])
    patterns.recompute(db)
    p = pattern(db, PID)
    assert (p["status"], p["user_state"], p["returned"], p["n_calls"]) == ("retired", "retired", 0, 4)
    assert patterns.for_prompt(db, "prep") == []
    patterns.set_user_state(db, PID, None)                               # undo
    assert _label(db)[:2] == ("active", "emerging")
    with pytest.raises(patterns.ActionRefused):
        patterns.set_user_state(db, PID, "maybe")
    with pytest.raises(KeyError):
        patterns.set_user_state(db, "lp:seller:u:local:new:never_seen", "wrong")


def test_wrong_excludes_the_observations_and_recomputes(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, TAG, [(d1, 0, True), (d2, 1, True), (d1, 2, True)])
    patterns.recompute(db)
    assert _label(db)[:2] == ("active", "emerging")
    p = patterns.set_user_state(db, PID, "wrong")
    assert (p["status"], p["n_calls"], p["n_deals"], p["label"]) == ("retired", 0, 0, "")
    assert db.execute("SELECT COUNT(*) FROM pattern_observations WHERE key=? AND excluded=1", (TAG,)).fetchone()[0] == 3
    calls_with_tag(db, TAG, [(d2, 3, True)])                             # a later sighting is excluded too
    patterns.recompute(db)
    assert pattern(db, PID)["n_calls"] == 0
    assert db.execute("SELECT COUNT(*) FROM pattern_observations WHERE key=? AND excluded=0", (TAG,)).fetchone()[0] == 0
    # Review 3: the legacy seller_patterns row mirrors the verdict as 'retired' (its counts stay), so the
    # prep brief's fallback and the Today card cannot carry what the user said is wrong.
    seller_memory.recompute(db)
    row = db.execute("SELECT status, calls_seen FROM seller_patterns WHERE tag=?", (TAG,)).fetchone()
    assert (row["status"], row["calls_seen"]) == ("retired", 4)


def test_recompute_is_idempotent(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, TAG, [(d1, 0, True), (d2, 1, True), (d1, 2, True)])
    calls_with_tag(db, "new:pitching_before_understanding", [(d1, 3, True)])
    sent_edit(db, d1, "Hi Arjun,\n\nGreat to meet you!\n\nThanks,\nS", "Hi Arjun,\n\nGood to meet you.\n\nThanks,\nS")
    first = patterns.recompute(db)
    snap = lambda t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1")]            # noqa: E731
    before = {t: snap(t) for t in ("pattern_observations", "learned_patterns", "learning_proposals")}
    second = patterns.recompute(db)
    assert first["changed"] >= 3 and first["new_proposals"] == 1
    assert (second["changed"], second["removed"], second["new_proposals"]) == (0, 0, 0)
    assert second["observations"] == {"added": 0, "updated": 0, "removed": 0, "rows": first["observations"]["rows"]}
    assert before == {t: snap(t) for t in before}


# ---- seller series ----------------------------------------------------------------------------------------

def test_series_come_from_the_final_snapshot_of_two_channel_calls_only(db):
    deal = make_deal(db, "A")
    live = make_call(db, deal, 0, source="capture")
    stereo = make_call(db, deal, 1, source="audio_file", layout="stereo_me_left")
    mono = make_call(db, deal, 2, source="audio_file", layout="mono_them")
    text = make_call(db, deal, 3, source="paste")
    final_snapshot(db, live, "replay-9", me_share=0.9, questions=1)     # a replay of the same call: the live run wins
    final_snapshot(db, live, "live-1", me_share=0.42, questions=7, known=2, partial=1)
    db.execute("INSERT INTO coach_state(call_id,session,t_call,json,created_at) VALUES (?,?,?,?,?)",
               (live, "live-1", 900.0, json.dumps({"me_share": 0.99, "me_questions": 99}), day(0)))   # not final
    final_snapshot(db, stereo, "replay-2", me_share=0.61, questions=3, known=0, partial=0)
    final_snapshot(db, mono, "replay-3", me_share=0.0)
    final_snapshot(db, text, "replay-4", me_share=0.5)
    patterns.recompute(db)
    got = {s["key"]: s for s in patterns.series(db)}
    assert set(got) == {"talk_share", "questions_asked", "slots_filled"}
    share = got["talk_share"]
    assert share["n"] == 2 and [p["value"] for p in share["points"]] == [0.42, 0.61]
    assert [p["from_replay"] for p in share["points"]] == [False, True]
    assert [p["value"] for p in got["slots_filled"]["points"]] == [3.0, 0.0]
    assert [p["value"] for p in patterns.series(db, "questions_asked")[0]["points"]] == [7.0, 3.0]
    assert db.execute("SELECT COUNT(*) FROM learned_patterns WHERE family='seller_series'").fetchone()[0] == 0


# ---- email voice ----------------------------------------------------------------------------------------------

CUSTOMER = "Arjun said the Bhiwandi lanes cost them eleven percent more than last year"
DRAFT = (f"Hi Arjun,\n\nI hope this email finds you well. {CUSTOMER}, which is exactly the pattern we discussed! "
         "I wanted to put down what we agreed and the three numbers you said you would send across by Friday.\n\n"
         "Please let me know if you have any questions.\n\nBest regards,\nMaya")
FINAL = (f"Hi Arjun,\n\n{CUSTOMER}. Here is what we agreed and the three numbers you said you would send by "
         "Friday.\n\nThanks,\nMaya")


def test_voice_heuristics_yield_at_most_one_rule_in_a_fixed_order():
    assert voice.derive_rule(DRAFT, FINAL)["key"] == "remove_phrase:hope_finds_you_well"
    no_filler = DRAFT.replace("I hope this email finds you well. ", "")
    assert voice.derive_rule(no_filler, FINAL)["key"] == "signoff:thanks"
    same_signoff = no_filler.replace("Best regards,", "Thanks,")
    assert voice.derive_rule(same_signoff, FINAL)["key"] == "drop_closing_line"
    no_closing = same_signoff.replace("Please let me know if you have any questions.\n\n", "")
    assert voice.derive_rule(no_closing, FINAL)["key"] == "no_exclamation"
    long = "Hi Arjun,\n\n" + " ".join(["word"] * 60) + "\n\nThanks,\nS"
    short = "Hi Arjun,\n\n" + " ".join(["word"] * 30) + "\n\nThanks,\nS"
    assert voice.derive_rule(long, short)["key"] == "shorten" and voice.derive_rule(short, long)["key"] == "lengthen"
    assert voice.derive_rule(long, long.replace("word", "term", 1)) is None        # a reworded word teaches no rule
    assert voice.derive_rule("Dear Arjun,\n\nHello there friend", "Arjun,\n\nHello there friend")["key"] == "drop_greeting"
    assert voice.derive_rule(DRAFT, DRAFT) is None and voice.derive_rule("", FINAL) is None


def test_email_voice_active_at_three_edits_or_on_confirm_and_holds_no_email_text(db):
    deal = make_deal(db, "A")
    sent_edit(db, deal, DRAFT, FINAL, sent_at=day(1))
    sent_edit(db, deal, DRAFT, FINAL, sent_at=day(2))
    patterns.recompute(db)
    pid = patterns.pattern_id("email_voice", "remove_phrase:hope_finds_you_well")
    p = pattern(db, pid)
    assert (p["status"], p["n_obs"]) == ("candidate", 2)
    assert p["summary"] == 'Do not write "i hope this email finds you well".'
    assert patterns.for_prompt(db, "email_drafter") == []
    sent_edit(db, deal, DRAFT, FINAL, sent_at=day(3))
    patterns.recompute(db)
    assert pattern(db, pid)["status"] == "active"
    got = patterns.for_prompt(db, "nudge_drafter")
    assert [(g["id"], g["n_obs"]) for g in got] == [(pid, 3)]
    assert db.execute("SELECT COUNT(*) FROM pattern_observations WHERE family='email_voice'").fetchone()[0] == 3

    # One edit, confirmed by the user, is active too.
    sent_edit(db, deal, "Hi A,\n\nGreat news!\n\nThanks,\nS", "Hi A,\n\nGood news.\n\nThanks,\nS", sent_at=day(4))
    patterns.recompute(db)
    bang = patterns.pattern_id("email_voice", "no_exclamation")
    assert pattern(db, bang)["status"] == "candidate"
    assert patterns.set_user_state(db, bang, "confirmed")["status"] == "active"

    # A saved-but-unsent edit teaches nothing.
    unsent = sent_edit(db, deal, "Hi A,\n\nKindly revert.\n\nThanks,\nS", "Hi A,\n\nPlease reply.\n\nThanks,\nS")
    db.execute("UPDATE emails SET status='saved_to_gmail' WHERE id=?", (unsent,))
    patterns.recompute(db)
    assert pattern(db, patterns.pattern_id("email_voice", "remove_phrase:kindly")) is None

    # No sentence, name or figure from either body is stored anywhere in the rule or its evidence.
    stored = " ".join(str(v) for r in db.execute("SELECT key, evidence FROM pattern_observations WHERE family='email_voice'")
                      for v in tuple(r))
    stored += " ".join(str(v) for r in db.execute("SELECT key, summary, stats FROM learned_patterns WHERE family='email_voice'")
                       for v in tuple(r))
    for needle in (CUSTOMER, "Arjun", "Bhiwandi", "eleven percent", "three numbers", "Great news", "Maya"):
        assert needle.lower() not in stored.lower(), needle


# ---- follow-up effectiveness -------------------------------------------------------------------------------------

def _sent_nudges(db, deal, loop, n, replied, start=0):
    for i in range(start, start + n):
        sent_at = f"2026-07-{7 + (i % 3) * 7:02d}T06:00:00+00:00"        # Tuesdays in July 2026
        email = make_email(db, deal, status="sent", sent_at=sent_at, thread_id=f"t{i}")
        link_nudge(db, loop, email, deal)
        if i - start < replied:
            db.execute("INSERT INTO email_replies(message_id,thread_id,email_id,deal_id,from_addr,received_at,body,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (f"m{i}", f"t{i}", email, deal, "b@x.test", sent_at[:8] + f"{8 + (i % 3) * 7:02d}T06:00:00+00:00", "ok", day(0)))


def test_followup_buckets_show_counts_and_a_rate_only_from_n_20(db):
    deal = make_deal(db, "A")
    loop = make_loop(db, deal)
    _sent_nudges(db, deal, loop, 19, replied=8)
    outcomes.recompute(db, today=date(2026, 9, 1))
    patterns.recompute(db)
    pid = patterns.pattern_id("followup", "weekday:tue")
    p = pattern(db, pid)
    stats = json.loads(p["stats"])
    assert (stats["sent"], stats["replied"], stats["no_reply"], stats["decided"], stats["rate"]) == (19, 8, 11, 19, None)
    assert p["status"] == "candidate" and "rate" not in p["summary"] and "n=19" in p["summary"]
    assert pattern(db, patterns.pattern_id("followup", "seq:4+"))["n_obs"] == 16

    _sent_nudges(db, deal, loop, 1, replied=1, start=19)
    outcomes.recompute(db, today=date(2026, 9, 1))
    patterns.recompute(db)
    p = pattern(db, pid)
    stats = json.loads(p["stats"])
    assert (stats["decided"], stats["rate"]) == (20, 0.45) and "reply rate 45%" in p["summary"]
    # Not a prompt input in this phase, whatever its n.
    for target in ("prep", "live_coach", "email_drafter", "nudge_drafter", "strategist"):
        assert all(g["family"] != "followup" for g in patterns.for_prompt(db, target, limit=50))


def test_a_pending_nudge_is_not_counted_as_silence(db):
    deal = make_deal(db, "A")
    loop = make_loop(db, deal)
    email = make_email(db, deal, status="sent", sent_at="2026-07-07T06:00:00+00:00")
    link_nudge(db, loop, email, deal)
    outcomes.recompute(db, today=date(2026, 7, 9))                        # inside the 5-business-day window
    patterns.recompute(db)
    stats = json.loads(pattern(db, patterns.pattern_id("followup", "weekday:tue"))["stats"])
    assert (stats["pending"], stats["no_reply"], stats["decided"]) == (1, 0, 0)
    assert observe.gap_bucket(4, [[0, 2], [3, 5], [11, None]]) == "3-5" and observe.gap_bucket(40, [[11, None]]) == "11+"


# ---- live-nudge usefulness ---------------------------------------------------------------------------------------------

def test_trigger_proposal_only_from_15_shown_and_never_auto_applied(db, monkeypatch):
    deal = make_deal(db, "A")
    call = make_call(db, deal, 0, source="capture")
    for i in range(14):
        live_nudge(db, call, "dig_deeper", outcome="ignored", dismissed=int(i < 2))
    patterns.recompute(db)
    assert patterns.open_proposals(db) == []
    live_nudge(db, call, "dig_deeper", outcome="followed")
    live_nudge(db, call, "objection", outcome="followed")                 # a useful trigger proposes nothing
    patterns.recompute(db)
    [prop] = patterns.open_proposals(db)
    assert prop["kind"] == "trigger_weight" and prop["payload"]["trigger"] == "dig_deeper"
    assert (prop["payload"]["shown"], prop["payload"]["ignored"], prop["payload"]["dismissed"]) == (15, 12, 2)
    assert (prop["payload"]["current_weight"], prop["payload"]["proposed_weight"]) == (0.75, 0.6)
    assert "shown 15 times live" in prop["summary"]
    before = (config.CONFIG_DIR / "live_coach.yaml").read_text()
    assert config.load("live_coach")["scoring"]["weights"]["dig_deeper"] == 0.75      # nothing applied by itself

    # Accepting writes the user's overlay (phase A's config.save_user); the tracked file is never edited.
    assert patterns.resolve_proposal(db, prop["id"], accept=True) == {"status": "accepted", "applied": "config"}
    assert (config.CONFIG_DIR / "live_coach.yaml").read_text() == before
    assert config.load_user("live_coach") == {"scoring": {"weights": {"dig_deeper": 0.6}}}
    assert patterns.open_proposals(db) == []
    patterns.recompute(db)
    assert patterns.open_proposals(db) == []                               # decided once, not re-proposed
    with pytest.raises(patterns.ActionRefused):
        patterns.resolve_proposal(db, prop["id"], accept=False)


def test_accepting_writes_the_user_overlay_when_the_settings_layer_exists(db, monkeypatch, tmp_path):
    deal = make_deal(db, "A")
    call = make_call(db, deal, 0, source="capture")
    for _ in range(15):
        live_nudge(db, call, "root_cause", outcome="ignored")
    patterns.recompute(db)
    [prop] = patterns.open_proposals(db)
    (tmp_path / "live_coach.yaml").write_text("scoring:\n  weights:\n    objection: 0.95\n")
    saved = {}
    monkeypatch.setattr(config, "user_dir", lambda: tmp_path, raising=False)
    monkeypatch.setattr(config, "save_user", lambda name, data: saved.update({name: data}), raising=False)
    assert patterns.resolve_proposal(db, prop["id"], accept=True)["applied"] == "config"
    assert saved == {"live_coach": {"scoring": {"weights": {"objection": 0.95, "root_cause": 0.52}}}}

    for _ in range(15):
        live_nudge(db, call, "status_quo", outcome="ignored")
    patterns.recompute(db)
    [other] = patterns.open_proposals(db)
    assert patterns.resolve_proposal(db, other["id"], accept=False) == {"status": "dismissed", "applied": None}
    assert "status_quo" not in saved["live_coach"]["scoring"]["weights"]


# ---- personas and objections: observation-only -------------------------------------------------------------------------

def _persona_deals(db, n, title="CFO", start=0):
    from salescoach import repo
    for i in range(start, start + n):
        deal = make_deal(db, f"P{i}")
        person = repo.create_person(db, f"Buyer {i}", email=f"buyer{i}@p{i}.test", title=title)
        repo.link_deal_person(db, deal, person, role="economic_buyer")


def test_persona_and_objection_are_never_promoted_below_the_deal_minimum(db):
    assert observe.persona_bucket("Chief Financial Officer") == "finance" and observe.persona_bucket("VP Sourcing") == "procurement"
    assert observe.persona_bucket("Head of Happiness") is None
    _persona_deals(db, 7)
    patterns.recompute(db)
    pid = patterns.pattern_id("persona", "finance")
    p = pattern(db, pid)
    assert (p["status"], p["label"], p["n_deals"]) == ("candidate", "", 7)             # 7 deals: below the minimum of 8
    assert patterns.for_prompt(db, "strategist") == []
    _persona_deals(db, 1, title="Head of Procurement", start=7)
    patterns.recompute(db)
    assert pattern(db, pid)["status"] == "active"                                      # 8 deals, 7 in the bucket
    assert pattern(db, patterns.pattern_id("persona", "procurement"))["status"] == "candidate"   # 1 deal in its bucket
    assert [g["key"] for g in patterns.for_prompt(db, "strategist")] == ["finance"]
    assert patterns.for_prompt(db, "strategist", deal_id="deal-without-a-cfo") == []

    # Objections: enough calls and deals to be "emerging" by the call rule, but only 3 deals carry one.
    deals = [make_deal(db, f"O{i}") for i in range(3)]
    for n, deal in enumerate(deals):
        call = make_call(db, deal, n, source="capture")
        final_snapshot(db, call, objections=("price", "price", "timing"))
        db.execute("INSERT INTO claims(call_id,deal_id,agent,subject,statement,kind,confidence,created_at) VALUES "
                   "(?,?,'call_analyst','deal.competition','They use the incumbent TMS','fact','high',?)", (call, deal, day(n)))
    patterns.recompute(db)
    price = pattern(db, patterns.pattern_id("objection", "price"))
    assert (price["n_calls"], price["n_deals"], price["status"], price["label"]) == (3, 3, "candidate", "")
    assert pattern(db, patterns.pattern_id("objection", "incumbent"))["n_calls"] == 3  # from the analyst's claims
    text = " ".join(r["evidence"] for r in db.execute("SELECT evidence FROM pattern_observations WHERE family='objection'"))
    assert "buyer's words" not in text and "incumbent TMS" not in text


# ---- tag hygiene ----------------------------------------------------------------------------------------------------------

def test_merge_is_proposed_never_automatic_and_accepting_folds_the_counts(db):
    d1, d2 = make_deal(db, "A"), make_deal(db, "B")
    calls_with_tag(db, "pitch_before_understanding", [(d1, 0, True), (d2, 1, True)])
    calls_with_tag(db, "new:pitching_before_understanding_problem", [(d1, 2, True)])
    calls_with_tag(db, "new:multilingual_rapport", [(d1, 3, True)], polarity="strength")
    patterns.recompute(db)
    target, source = patterns.pattern_id("seller", "pitch_before_understanding"), \
        patterns.pattern_id("seller", "new:pitching_before_understanding_problem")
    [prop] = patterns.open_proposals(db)
    assert prop["kind"] == "merge" and (prop["pattern_id"], prop["target_id"]) == (source, target)
    assert pattern(db, source)["merged_into"] is None and pattern(db, target)["n_calls"] == 2      # nothing merged yet
    assert patterns.similarity("new:pitching_before_understanding_problem", "pitch_before_understanding") == 0.75
    assert patterns.similarity("new:multilingual_rapport", "pitch_before_understanding") == 0.0

    patterns.resolve_proposal(db, prop["id"], accept=True)
    assert pattern(db, source)["merged_into"] == target and pattern(db, source)["status"] == "retired"
    assert _label(db, target) == ("active", "emerging", 3, 2)                         # 2 + 1 calls, folded
    prov = db.execute("SELECT confidence FROM field_provenance WHERE entity_id=? AND field='merged_into'", (source,)).fetchone()
    assert prov["confidence"] == "user_input"
    patterns.recompute(db)
    assert patterns.open_proposals(db) == [] and _label(db, target)[2] == 3

    with pytest.raises(patterns.ActionRefused):
        patterns.merge_into(db, target, source)                                        # would be a cycle
    with pytest.raises(patterns.ActionRefused):
        patterns.merge_into(db, target, target)
    patterns.merge_into(db, source, None)                                              # unmerge
    assert _label(db, target)[2] == 2 and pattern(db, source)["status"] == "candidate"


def test_a_dismissed_merge_is_not_proposed_again_and_new_tags_pair_up(db):
    d1 = make_deal(db, "A")
    calls_with_tag(db, "new:talks_over_the_buyer", [(d1, 0, True), (d1, 1, True)])
    calls_with_tag(db, "new:talking_over_buyer", [(d1, 2, True)])
    patterns.recompute(db)
    [prop] = patterns.open_proposals(db)
    assert prop["payload"]["from"] == "new:talking_over_buyer" and prop["payload"]["into"] == "new:talks_over_the_buyer"
    patterns.resolve_proposal(db, prop["id"], accept=False)
    patterns.recompute(db)
    assert patterns.open_proposals(db) == []
    assert pattern(db, patterns.pattern_id("seller", "new:talking_over_buyer"))["merged_into"] is None


def test_merge_into_a_taxonomy_tag_nobody_has_been_seen_doing_yet(db):
    d1 = make_deal(db, "A")
    calls_with_tag(db, "new:no_decision_process_established", [(d1, 0, True)])
    patterns.recompute(db)
    target = patterns.pattern_id("seller", "no_decision_process")
    assert pattern(db, target) is None
    [prop] = patterns.open_proposals(db)
    assert prop["target_id"] == target
    patterns.resolve_proposal(db, prop["id"], accept=True)
    p = pattern(db, target)
    assert (p["n_calls"], p["status"], p["summary"]) == (1, "candidate", "Not establishing the decision process")
    assert pattern(db, patterns.pattern_id("seller", "new:no_decision_process_established"))["n_calls"] == 0


# ---- for_prompt ------------------------------------------------------------------------------------------------------------------

def test_for_prompt_returns_only_active_unretired_patterns_capped(db):
    deals = [make_deal(db, n) for n in "ABC"]
    tags = ["avoids_budget", "no_decision_process", "leaves_open_loops", "over_explains_technical"]
    for n in range(8):
        call = make_call(db, deals[n % 3], n)
        for t in tags[: 4 if n < 6 else 1]:                                 # avoids_budget on all 8, the rest on 6
            tag(db, call, t)
    lonely = make_call(db, deals[0], 8)
    tag(db, lonely, "new:one_off_thing")
    patterns.recompute(db)
    got = patterns.for_prompt(db, "prep")
    assert len(got) == 3 and got[0]["key"] == "avoids_budget"
    assert set(got[0]) == {"id", "family", "key", "summary", "label", "n_calls", "n_deals", "n_obs"}
    assert got[0] == {"id": PID, "family": "seller", "key": "avoids_budget", "summary": "Avoiding the budget conversation",
                      "label": "established", "n_calls": 8, "n_deals": 3, "n_obs": 8}
    assert all(g["label"] in ("emerging", "established") for g in patterns.for_prompt(db, "live_coach", limit=10))
    keys = {g["key"] for g in patterns.for_prompt(db, "prep", limit=10)}
    assert keys == set(tags)                                                 # the one-off candidate is never offered
    patterns.set_user_state(db, PID, "wrong")
    patterns.set_user_state(db, patterns.pattern_id("seller", "no_decision_process"), "retired")
    keys = {g["key"] for g in patterns.for_prompt(db, "prep", limit=10)}
    assert keys == {"leaves_open_loops", "over_explains_technical"}
    assert patterns.for_prompt(db, "prep", limit=0) == [] and patterns.for_prompt(db, "no_such_target") == []
    assert patterns.for_prompt(db, "email_drafter") == []                     # other families only
