"""Hub, level meters, bleed detection and the live transcriber queue."""
import threading

import numpy as np
import pytest

from salescoach.live import stream_asr
from salescoach.live.echo import flag_bleed, is_bleed
from salescoach.live.hub import Hub, topic_for
from salescoach.live.levels import Levels
from salescoach.live.stream_asr import LiveTranscriber
from salescoach.live.vad import SpeechSegment
from salescoach.providers.base import Segment
from salescoach.speech.models import FakeTranscriber, ModelNotAvailable


# ---- hub ----------------------------------------------------------------------

def test_hub_drops_oldest_and_tracks_latest():
    hub = Hub()
    topic = topic_for("call-1")
    q = hub.subscribe(topic, maxsize=3)
    for i in range(5):
        assert hub.publish(topic, {"type": "level", "i": i}) == 1
    assert [q.get_nowait()["i"] for _ in range(3)] == [2, 3, 4]
    assert hub.dropped == 2
    assert hub.latest(topic)["level"]["i"] == 4 and "ts" in hub.latest(topic)["level"]
    hub.unsubscribe(topic, q)
    assert hub.publish(topic, {"type": "level"}) == 0 and hub.subscribers(topic) == 0


def test_hub_concurrent_publishers():
    hub = Hub()
    q = hub.subscribe("t", maxsize=10000)
    threads = [threading.Thread(target=lambda: [hub.publish("t", {"type": "x"}) for _ in range(500)])
               for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert q.qsize() == 2000


# ---- levels -------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_levels_and_silence_alert_is_edge_triggered():
    clock = Clock()
    levels = Levels(16000, silence_alert_s=60, clock=clock)
    tone = (0.3 * 32767 * np.sin(np.arange(4800) / 16000 * 2 * np.pi * 440)).astype(np.int16)
    levels.update("me", tone)
    snap = levels.snapshot()
    assert snap["me"]["rms_dbfs"] == pytest.approx(-13.5, abs=0.5) and snap["me"]["receiving"]
    assert snap["them"]["rms_dbfs"] == -120.0 and not snap["them"]["receiving"]
    clock.t = 30.0
    levels.update("me", tone)
    clock.t = 60.0
    assert levels.check_silence() == ["them"]              # never produced sound
    clock.t = 90.0
    assert levels.check_silence() == ["me"]
    assert levels.check_silence() == []                    # fires once
    levels.update("me", tone)                              # sound re-arms the alarm
    clock.t = 150.0
    assert levels.check_silence() == ["me"]


# ---- echo ---------------------------------------------------------------------

def test_flag_bleed():
    turns = [
        {"channel": "them", "t_start": 1.0, "t_end": 3.0, "text": "We need the proposal by Friday, please."},
        {"channel": "me", "t_start": 1.5, "t_end": 3.2, "text": "we need the proposal by friday please"},
        {"channel": "me", "t_start": 9.0, "t_end": 10.0, "text": "We need the proposal by Friday please"},  # too far
        {"channel": "them", "t_start": 12.0, "t_end": 12.5, "text": "Okay."},
        {"channel": "me", "t_start": 12.1, "t_end": 12.4, "text": "okay"},                   # too short to judge
        {"channel": "me", "t_start": 2.0, "t_end": 2.5, "text": "the proposal by friday"},     # fragment
    ]
    assert flag_bleed(turns) == [1, 5]
    assert turns[1]["bleed_flag"] == 1 and "bleed_flag" not in turns[2]
    assert not is_bleed("totally different words here", "we need the proposal by friday")


# ---- live transcriber ---------------------------------------------------------

def _speech(start_s, seconds=1.0, channel="me"):
    n = int(seconds * 16000)
    return SpeechSegment(channel, int(start_s * 16000), int(start_s * 16000) + n,
                         np.full(n, 1000, dtype=np.int16))


def test_live_transcriber_absolute_times():
    got = []
    fake = FakeTranscriber([[{"t_start": 0.1, "t_end": 0.9, "text": " hello ", "avg_logprob": -0.1}], [
        {"t_start": 0.0, "t_end": 0.5, "text": "   "}]])
    live = LiveTranscriber(fake, got.append).start()
    live.submit(_speech(2.0))
    live.submit(_speech(5.0, channel="them"))
    stats = live.stop(drain=True, timeout=5)
    assert [(s.channel, s.t_start, s.t_end, s.text) for s in got] == [("me", 2.1, 2.9, "hello")]
    assert stats["transcribed"] == 2 and stats["segments"] == 1


def test_live_transcriber_disables_on_missing_model():
    class Missing:
        model = "x"

        def transcribe(self, audio, word_timestamps=False):
            raise ModelNotAvailable("org/model")

    errors = []
    live = LiveTranscriber(Missing(), lambda s: None, on_error=lambda k, m: errors.append((k, m))).start()
    for i in range(3):
        live.submit(_speech(i))
    stats = live.stop(timeout=5)
    assert [k for k, _ in errors] == ["asr_unavailable"]
    assert "salescoach models pull org/model" in errors[0][1]
    assert stats["skipped"] == 2 and live.disabled


def test_live_transcriber_drops_oldest_when_behind():
    gate = threading.Event()

    class Slow(FakeTranscriber):
        def transcribe(self, audio, word_timestamps=False):
            gate.wait(5)
            return super().transcribe(audio, word_timestamps)

    got, errors = [], []
    live = LiveTranscriber(Slow(), got.append, max_queue=2,
                           on_error=lambda k, m: errors.append(k)).start()
    for i in range(6):
        live.submit(_speech(i))
    gate.set()
    stats = live.stop(timeout=5)
    assert stats["dropped"] >= 3 and "asr_backlog" in errors
    assert got[-1].t_start == pytest.approx(5.0)            # the newest survives


def test_whisperlivekit_engine_not_wired():
    with pytest.raises(NotImplementedError, match="bake-off"):
        stream_asr.create("auto", lambda s: None, cfg={"live": {"engine": "whisperlivekit"}})
    live = stream_asr.create("auto", lambda s: None, transcriber=FakeTranscriber(), cfg={"live": {"engine": "chunked"}})
    assert live.model == "fake"
