"""Phase 2 fast path: each of the nine triggers fires on an English and a
Hinglish example, stays silent on neutral talk and on its near miss, names
the entity when it knows it, and stays far under the 50 ms budget. The
optional local classifier never blocks."""
import threading
import time

import pytest

from salescoach.coach import settings
from salescoach.coach.detectors import MAX_WORDS, TRIGGERS, FastDetectors, LocalClassifier
from salescoach.coach.state import ConversationState, Seg
from salescoach.coach.text import Vocab

CASES = {
    "dig_deeper": {
        "en": [("them", "The real issue is that our planners don't trust the data they get from transporters."),
               ("me", "Okay, so moving on, let me talk about pricing and the next steps.")],
        "hi": [("them", "Sabse badi dikkat yeh hai ki transporter time pe gaadi nahi bhejte."),
               ("me", "Accha chaliye, pricing ke baare mein baat karte hain ab.")]},
    "quantify_impact": {
        "en": [("them", "Detention on month-end shipments is a big problem for us."),
               ("me", "Got it. Let me show you how the platform plans loads.")],
        "hi": [("them", "Month end pe detention ka bahut problem hota hai trucks mein."),
               ("me", "Accha sir, main aapko hamara dashboard dikhata hoon.")]},
    "root_cause": {
        "en": [("them", "Trucks keep getting delayed at the plant, it is a problem every time the month closes.")],
        "hi": [("them", "Har baar gaadi late ho jaati hai plant pe, dispatch mein dikkat hoti hai.")]},
    "status_quo": {
        "en": [("them", "Honestly we are not happy with how freight billing works, it is a mess.")],
        "hi": [("them", "Freight billing se bahut pareshani hai, invoice mein galat hota hai.")]},
    "buying_process": {
        "en": [("them", "This will need approval from the purchase committee.")],
        "hi": [("them", "Iske liye approval lena padega upar se, tab hoga.")]},
    "stakeholder_gap": {
        "en": [("them", "I will need to take this to our CFO before anything moves.")],
        "hi": [("them", "Yeh presentation sir ko dikhana padega pehle.")]},
    "weak_commitment": {
        "en": [("them", "Let's see, maybe we can set up a meeting next week sometime.")],
        "hi": [("them", "Theek hai sir, dekhte hain, meeting karenge next week.")]},
    "buying_signal": {
        "en": [("them", "When we implement this, our team would use it for every dispatch.")],
        "hi": [("them", "Agar yeh chalu hua toh hamare log use karenge daily planning ke liye.")]},
    "objection": {
        "en": [("them", "To be frank, we already have a vendor doing this for us.")],
        "hi": [("them", "Sir yeh toh bahut mehenga hai, budget nahi hai abhi.")]},
}

NEUTRAL = {
    "en": [("them", "Good afternoon, can you see my screen now?"), ("me", "Yes, I can see it clearly, thank you."),
           ("them", "Great. We deliver to about forty customers in the north region."),
           ("me", "Understood. Which plants do those loads go out from?")],
    "hi": [("them", "Haan sir, sab theek hai, aap boliye, main sun raha hoon."),
           ("me", "Ji sir, main aapko ek chhota sa overview deta hoon."),
           ("them", "Accha sir, theek hai, aage boliye.")],
}

# a line or exchange that sounds like the trigger but must not fire it
NEAR_MISS = {
    "dig_deeper": [("them", "The real issue is that our planners don't trust the transporter data."),
                   ("me", "Why don't the planners trust the transporter data today?")],
    "quantify_impact": [("them", "Detention is a big problem for our trucks."),
                        ("them", "It costs us about 40 lakh every month."), ("me", "That is a lot, let me note that down.")],
    "root_cause": [("them", "We have a problem with freight cost.")],
    "status_quo": [("them", "The billing mess is a problem and the board wants it fixed this quarter.")],
    "buying_process": [("them", "We deliver to forty customers a day from two plants.")],
    "stakeholder_gap": [("them", "Arjun ji will review the numbers with our CFO.")],
    "weak_commitment": [("them", "I will send you the data by Friday 5 pm.")],
    "buying_signal": [("them", "Can you share the deck again after the call?")],
    "objection": [("them", "No doubt, the pricing looks fair to us.")],
}


@pytest.fixture(scope="module")
def cfg():
    return settings.load()


def run(cfg, lines, known=(), t0=100.0):
    vocab = Vocab(cfg)
    state = ConversationState(cfg, vocab, known)
    det = FastDetectors(cfg, vocab)
    out, t = [], t0
    for i, (channel, text) in enumerate(lines):
        seg = Seg(i, channel, t, t + 4, text)
        t += 5
        state.add(seg)
        out += det.detect(state, seg)
    return out, state


@pytest.mark.parametrize("lang", ["en", "hi"])
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_trigger_fires(cfg, trigger, lang):
    out, _ = run(cfg, CASES[trigger][lang])
    hits = [c for c in out if c.trigger == trigger]
    assert hits, f"{trigger}/{lang} did not fire; got {[c.trigger for c in out]}"
    c = hits[0]
    assert len(c.text.split()) <= MAX_WORDS
    assert 0 < c.confidence <= 1 and c.kind == cfg["triggers"][trigger]["kind"]
    assert c.anchor_text and c.source == "fast"


@pytest.mark.parametrize("lang", ["en", "hi"])
def test_neutral_talk_is_silent(cfg, lang):
    out, _ = run(cfg, NEUTRAL[lang])
    assert out == []


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_near_miss_does_not_fire(cfg, trigger):
    out, _ = run(cfg, NEAR_MISS[trigger], known=("Arjun Kumar", "CFO"))
    assert trigger not in [c.trigger for c in out]


def test_entity_specialises_the_text(cfg):
    out, _ = run(cfg, [("them", "I will need to take this to our CFO before anything moves.")])
    assert next(c for c in out if c.trigger == "stakeholder_gap").text == "Ask how the CFO is involved in this."
    out, _ = run(cfg, [("them", "The CFO has to approve anything above ten lakh.")])
    assert next(c for c in out if c.trigger == "buying_process").text == "Ask how the CFO signs off on this."
    out, _ = run(cfg, [("them", "Honestly it is too expensive for us.")])
    assert next(c for c in out if c.trigger == "objection").entity == "price"


def test_known_stakeholder_is_not_a_gap(cfg):
    out, state = run(cfg, [("them", "Our CFO will join next time.")], known=("Rakesh Shah", "CFO"))
    assert "stakeholder_gap" not in [c.trigger for c in out]
    assert state.snapshot()["stakeholders_named"][0]["known"] is True


def test_me_asking_marks_the_slot(cfg):
    _, state = run(cfg, [("them", "Detention is a big problem for our trucks."),
                         ("me", "How much does that cost you every month?")])
    assert state.slots["impact"].status == "asked" and state.slots["impact"].asks == 1
    assert state.me_questions == 1


def test_fast_path_is_well_under_50ms(cfg):
    lines = [pair for case in CASES.values() for pair in case["en"] + case["hi"]] * 15     # 22 lines x 15
    vocab = Vocab(cfg)
    state, det = ConversationState(cfg, vocab), FastDetectors(cfg, vocab)
    worst, t = 0.0, 0.0
    for i, (channel, text) in enumerate(lines):
        seg = Seg(i, channel, t, t + 3, text)
        t += 4
        started = time.perf_counter()
        state.add(seg)
        det.detect(state, seg)
        worst = max(worst, time.perf_counter() - started)
    assert len(state.segments) > 300
    assert worst < 0.05, f"slowest segment took {worst * 1000:.1f} ms"


def test_local_classifier_is_off_by_default_and_never_blocks(cfg):
    got = []
    assert LocalClassifier(cfg, lambda *a: got.append(a)).enabled is False
    on = settings.merge(settings.load(), {"fast": {"local_model": {"enabled": True, "timeout_s": 0.2}}})
    release = threading.Event()

    def slow_model(text):
        release.wait(2)
        return "objection", 0.9

    lc = LocalClassifier(on, lambda *a: got.append(a), classify=slow_model)
    seg = Seg(1, "them", 0, 4, "hmm we will have to think about how this fits with everything else")
    vocab = Vocab(on)
    ConversationState(on, vocab).prepare(seg)
    started = time.perf_counter()
    assert lc.wants(seg) and lc.submit(seg, 4.0) is True
    assert lc.submit(seg, 4.0) is False                 # one in flight; the second is skipped, not queued
    assert time.perf_counter() - started < 0.05
    time.sleep(0.3)                                     # let the model overrun its 0.2 s timeout
    release.set()
    deadline = time.time() + 3
    while lc._busy.locked() and time.time() < deadline:
        time.sleep(0.01)
    assert got == [] and lc.late == 1                   # answered after its timeout: discarded

    quick = LocalClassifier(on, lambda *a: got.append(a), classify=lambda text: ("buying_signal", 0.8))
    quick.submit(seg, 4.0)
    deadline = time.time() + 3
    while not got and time.time() < deadline:
        time.sleep(0.01)
    assert got and got[0][0] == "buying_signal"
