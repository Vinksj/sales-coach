import numpy as np
import pytest
import soundfile as sf

from salescoach import config, repo
from salescoach.speech.final import transcribe_call, transcript_sha, windows
from salescoach.speech.models import FakeTranscriber, MlxWhisperTranscriber, ModelNotAvailable
from salescoach.store import stores

RATE = 16000

ME = [
    {"t_start": 3.0, "t_end": 4.0, "text": "Our pricing starts at ten lakh", "avg_logprob": -0.3, "no_speech_prob": 0.05},
    {"t_start": 1.4, "t_end": 2.4, "text": "we need the proposal by friday please", "avg_logprob": -0.5,
     "no_speech_prob": 0.1},                                              # bleed of the them turn below
    {"t_start": 5.0, "t_end": 5.5, "text": "mumble mumble", "avg_logprob": -1.5, "no_speech_prob": 0.1},
    {"t_start": 4.2, "t_end": 4.8, "text": "sort of", "avg_logprob": -0.9, "no_speech_prob": 0.1},
]
THEM = [
    {"t_start": 1.0, "t_end": 2.2, "text": "We need the proposal by Friday, please.", "avg_logprob": -0.2,
     "no_speech_prob": 0.02},
    {"t_start": 4.5, "t_end": 5.2, "text": "hello?", "avg_logprob": -0.3, "no_speech_prob": 0.7},
]


def _tone(seconds, freq, amp=0.3):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("SALES_DB", str(tmp_path / "sales.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    conn = stores.sales()
    audio_dir = config.calls_dir() / "c1"
    audio_dir.mkdir()
    sf.write(audio_dir / "me.flac", _tone(6, 440), RATE, subtype="PCM_16")
    sf.write(audio_dir / "them.flac", _tone(6, 660), RATE, subtype="PCM_16")
    call_id = repo.create_call(conn, source="capture", title="t", audio_dir=str(audio_dir), wf_state="captured")
    conn.commit()
    yield conn, call_id, audio_dir
    conn.close()


def test_merge_quality_bleed_and_sha(call):
    conn, call_id, _ = call
    fake = FakeTranscriber([ME, THEM])                          # me channel is transcribed first
    assert transcribe_call(conn, call_id, transcriber_factory=lambda lang_mode: fake) == 6
    conn.commit()
    rows = repo.turns(conn, call_id, "final")
    assert [(r["idx"], r["channel"], r["t_start"]) for r in rows] == [
        (0, "them", 1.0), (1, "me", 1.4), (2, "me", 3.0), (3, "me", 4.2), (4, "them", 4.5), (5, "me", 5.0)]
    assert [r["quality"] for r in rows] == ["ok", "ok", "ok", "partial", "garbled", "garbled"]
    assert {r["quality_note"] for r in rows} == {"asr_confidence"}
    assert [r["bleed_flag"] for r in rows] == [0, 1, 0, 0, 0, 0]
    assert all(c["word_timestamps"] for c in fake.calls)
    row = repo.get_call(conn, call_id)
    assert row["wf_state"] == "final_transcribed" and row["asr_final_model"] == "fake"
    assert row["transcript_sha"] == transcript_sha([dict(r) for r in rows]) and len(row["transcript_sha"]) == 64

    # re-running replaces the final tier instead of appending to it
    assert transcribe_call(conn, call_id, transcriber_factory=lambda lm: FakeTranscriber([ME[:1], THEM[:1]])) == 2
    assert len(repo.turns(conn, call_id, "final")) == 2


def test_long_audio_is_windowed(call):
    conn, call_id, _ = call
    fake = FakeTranscriber()                                    # one segment spanning each window
    n = transcribe_call(conn, call_id, transcriber_factory=lambda lm: fake, window_s=2.5)
    assert len(fake.calls) == n >= 6
    assert all(c["samples"] <= 2.5 * RATE for c in fake.calls)
    assert sum(c["samples"] for c in fake.calls) == 2 * 6 * RATE
    for channel in ("me", "them"):
        turns = [r for r in repo.turns(conn, call_id, "final") if r["channel"] == channel]
        assert turns[0]["t_start"] == 0.0 and turns[-1]["t_end"] == pytest.approx(6.0)
        for prev, nxt in zip(turns, turns[1:]):
            assert nxt["t_start"] == pytest.approx(prev["t_end"], abs=1e-3)   # windows tile the channel


def test_windows_cut_at_quiet_point():
    audio = np.concatenate([_tone(2.0, 440), np.zeros(RATE // 2, np.float32), _tone(2.0, 440)])
    (a, cut), (cut2, end) = windows(audio, RATE, window_s=3.0, search_s=2.0)
    assert cut == cut2 and 2.0 * RATE <= cut <= 2.5 * RATE and end == len(audio)


def test_silent_channel_is_skipped(call):
    conn, call_id, audio_dir = call
    sf.write(audio_dir / "me.flac", np.zeros(6 * RATE, np.float32), RATE, subtype="PCM_16")
    fake = FakeTranscriber([THEM])
    assert transcribe_call(conn, call_id, transcriber_factory=lambda lm: fake) == 2
    assert len(fake.calls) == 1


def test_missing_model_fails_before_touching_turns(call, tmp_path):
    conn, call_id, _ = call
    transcribe_call(conn, call_id, transcriber_factory=lambda lm: FakeTranscriber([ME, THEM]))
    conn.commit()
    with pytest.raises(ModelNotAvailable):
        transcribe_call(conn, call_id,
                        transcriber_factory=lambda lm: MlxWhisperTranscriber("org/absent", cache_dir=tmp_path / "hf"))
    assert len(repo.turns(conn, call_id, "final")) == 6


def test_refuses_live_call(call):
    conn, call_id, _ = call
    repo.set_call_state(conn, call_id, "live")
    with pytest.raises(RuntimeError, match="still live"):
        transcribe_call(conn, call_id, transcriber_factory=lambda lm: FakeTranscriber())
