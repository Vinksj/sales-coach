import numpy as np
import pytest

from salescoach.live.vad import EnergyVAD

RATE = 16000


def _noise(seconds, rng):
    return rng.normal(0, 8, int(seconds * RATE))            # about -72 dBFS


def _tone(seconds, amp=0.3, freq=440):
    t = np.arange(int(seconds * RATE)) / RATE
    return amp * 32767 * np.sin(2 * np.pi * freq * t)


def _signal():
    rng = np.random.default_rng(0)
    parts = [_noise(1.0, rng), _tone(2.0), _noise(1.5, rng), _tone(2.0), _noise(2.0, rng),
             _tone(0.02, amp=0.5), _noise(2.48, rng), _tone(15.0), _noise(1.5, rng)]
    return np.clip(np.concatenate(parts), -32768, 32767).astype(np.int16)


def _run(vad, audio, chunk=337):
    out = []
    for i in range(0, len(audio), chunk):
        out += vad.feed(audio[i:i + chunk])
    return out + vad.flush()


def test_segments_on_synthetic_audio():
    vad = EnergyVAD("me", sample_rate=RATE, min_segment_s=1.2, max_segment_s=12.0, silence_s=0.6)
    segs = _run(vad, _signal())
    assert len(segs) == 4                                   # the 20 ms click is dropped
    first, second, long_a, long_b = segs
    assert first.t_start == pytest.approx(0.8, abs=0.06) and first.t_end == pytest.approx(3.2, abs=0.06)
    assert second.t_start == pytest.approx(4.3, abs=0.06) and second.t_end == pytest.approx(6.7, abs=0.06)
    # the 15 s burst (11.0 to 26.0) is split in two, neither over max_segment_s
    assert long_a.t_start == pytest.approx(10.8, abs=0.06) and long_b.t_end == pytest.approx(26.2, abs=0.06)
    assert long_a.end == long_b.start
    assert all(s.duration <= 12.0 + 0.03 for s in segs)
    assert all(len(s.audio) == s.end - s.start and s.channel == "me" for s in segs)
    assert vad.floor_db < -60                               # the floor came back down after the long tone


def test_gap_counts_as_silence():
    vad = EnergyVAD("them", sample_rate=RATE)
    tone = _tone(1.0).astype(np.int16)
    out = vad.feed(tone, start=0)
    out += vad.feed(tone, start=16000 + 48000)             # 3 s hole in the archive
    out += vad.flush()
    assert len(out) == 2
    # a gap closes the segment at the last real audio (no tail into the hole)
    # and leaves nothing to pre-roll from, so the next one starts on its onset
    assert out[0].t_start == pytest.approx(0.0) and out[0].t_end == pytest.approx(1.0, abs=0.04)
    assert out[1].t_start == pytest.approx(4.0, abs=0.04)


def test_short_dropout_stays_one_segment():
    vad = EnergyVAD("them", sample_rate=RATE)
    tone = _tone(1.0).astype(np.int16)
    out = vad.feed(tone, start=0) + vad.feed(tone, start=16000 + 1600) + vad.flush()   # 0.1 s dropout
    assert len(out) == 1
    assert out[0].t_end == pytest.approx(2.1, abs=0.04)
    assert not out[0].audio[16000:17600].any()


def test_from_config_reads_asr_yaml():
    vad = EnergyVAD.from_config("me", {"sample_rate": 16000, "live": {"min_segment_s": 2.0,
                                                                     "max_segment_s": 5.0, "silence_s": 0.4}})
    assert vad.min_segment == 32000 and vad.max_segment == 80000 and vad.silence == 6400
