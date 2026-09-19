import json

import numpy as np
import pytest
import soundfile as sf

from salescoach.live.archive import Archive, finalize_dir, read_meta, recover
from salescoach.live.frames import Frame

RATE = 16000
T0 = 5_000_000_000
FRAME = 320


def _tone(n, freq, amp=0.3):
    t = np.arange(n) / RATE
    return (amp * 32767 * np.sin(2 * np.pi * freq * t)).astype("<i2")


def _ts(sample):
    return T0 + int(sample * 1e9 / RATE)


def _abandon(archive):
    """Simulated crash: the process dies, nothing is finalized or closed."""
    for fh in archive._files.values():
        fh.close()


def test_recover_after_crash_aligns_channels(tmp_path):
    me, them = _tone(32000, 440), _tone(32000, 660)
    archive = Archive(tmp_path, sample_rate=RATE, fsync_interval_s=0)
    starts = {"me": [], "them": []}
    for i in range(0, 32000, FRAME):
        starts["me"].append(archive.write(Frame(0, _ts(i), me[i:i + FRAME].tobytes())))
        if i < 8000 or i >= 24000:                      # 'them' drops out for 1 s
            starts["them"].append(archive.write(Frame(1, _ts(i), them[i:i + FRAME].tobytes())))
    assert starts["me"][:3] == [0, 320, 640]
    assert 24000 in starts["them"]                     # the frame after the gap lands at its true time
    assert (tmp_path / "me.pcm").exists() and read_meta(tmp_path)["t0_ns"] == T0
    _abandon(archive)

    result = recover(tmp_path)
    assert result["duration_s"] == pytest.approx(2.0)
    me_f, sr = sf.read(tmp_path / "me.flac", dtype="int16")
    them_f, _ = sf.read(tmp_path / "them.flac", dtype="int16")
    assert sr == RATE and len(me_f) == len(them_f) == 32000
    assert np.array_equal(me_f, me)
    assert np.array_equal(them_f[:8000], them[:8000])
    assert not them_f[8000:24000].any()                # the 1 s gap is 1 s of silence
    assert np.array_equal(them_f[24000:], them[24000:])
    assert not (tmp_path / "me.pcm").exists() and not (tmp_path / "them.pcm").exists()
    assert read_meta(tmp_path)["finalized"] is True
    assert recover(tmp_path) is None                   # idempotent


def test_late_channel_and_equal_length(tmp_path):
    archive = Archive(tmp_path, sample_rate=RATE)
    archive.write(Frame(0, _ts(0), _tone(16000, 440).tobytes()))
    start = archive.write(Frame(1, _ts(4800), _tone(3200, 660).tobytes()))   # starts 0.3 s late
    assert start == 4800
    result = archive.finalize()
    them_f, _ = sf.read(tmp_path / "them.flac", dtype="int16")
    assert len(them_f) == 16000                        # padded to the longer channel
    assert not them_f[:4800].any() and them_f[4800:8000].any() and not them_f[8000:].any()
    assert result["samples"] == {"me": 16000, "them": 8000}


def test_small_jitter_is_not_padded(tmp_path):
    archive = Archive(tmp_path, sample_rate=RATE)
    archive.write(Frame(0, _ts(0), _tone(320, 440).tobytes()))
    assert archive.write(Frame(0, _ts(320 + 400), _tone(320, 440).tobytes())) == 320   # 25 ms late: append
    assert archive.stats["gaps"] == 0
    archive.close()


def test_torn_sample_and_missing_channel(tmp_path):
    (tmp_path / "me.pcm").write_bytes(_tone(1000, 440).tobytes() + b"\x01")
    (tmp_path / "meta.json").write_text(json.dumps({"sample_rate": RATE, "t0_ns": T0}))
    result = finalize_dir(tmp_path)
    assert result["samples"] == {"me": 1000}
    assert sf.info(str(tmp_path / "them.flac")).frames == 1000     # silent partner channel created
    assert not sf.read(tmp_path / "them.flac", dtype="int16")[0].any()


def test_resume_continues_timeline(tmp_path):
    first = Archive(tmp_path, sample_rate=RATE, fsync_interval_s=0)
    first.write(Frame(0, _ts(0), _tone(1600, 440).tobytes()))
    _abandon(first)
    second = Archive(tmp_path)                                     # same dir, e.g. callcap restarted
    assert second.rate == RATE and second.position("me") == 1600
    assert second.write(Frame(0, _ts(16000), _tone(1600, 440).tobytes())) == 16000
    second.finalize()
    assert sf.info(str(tmp_path / "me.flac")).frames == 17600
    with pytest.raises(RuntimeError):
        Archive(tmp_path)                                          # finalized archives are read-only
