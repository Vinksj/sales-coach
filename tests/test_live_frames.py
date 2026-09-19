import io

import numpy as np

from salescoach.live.frames import HEADER, encode_frame, read_frames


def _pcm(n, seed):
    return np.random.default_rng(seed).integers(-3000, 3000, n).astype("<i2")


class Trickle(io.RawIOBase):
    """A pipe that hands out a few bytes per read."""

    def __init__(self, data, step=7):
        self.data, self.pos, self.step = data, 0, step

    def readable(self):
        return True

    def read(self, n=-1):
        k = self.step if n is None or n < 0 else min(n, self.step)
        chunk = self.data[self.pos:self.pos + k]
        self.pos += len(chunk)
        return chunk


def test_round_trip():
    frames = [(0, 100, _pcm(320, 1)), (1, 200, _pcm(160, 2)), (0, 300, _pcm(1, 3))]
    data = b"".join(encode_frame(c, t, p) for c, t, p in frames)
    stats = {}
    got = list(read_frames(io.BytesIO(data), stats=stats))
    assert [(f.channel, f.host_time_ns) for f in got] == [(0, 100), (1, 200), (0, 300)]
    for frame, (_, _, pcm) in zip(got, frames):
        assert np.array_equal(frame.samples(), pcm)
    assert got[1].name == "them" and got[1].n == 160
    assert stats == {"frames": 3, "resyncs": 0, "skipped_bytes": 0, "truncated_tail": False}


def test_short_reads():
    frames = [(i % 2, i, _pcm(100, i)) for i in range(5)]
    data = b"".join(encode_frame(c, t, p) for c, t, p in frames)
    got = list(read_frames(Trickle(data)))
    assert [f.host_time_ns for f in got] == [0, 1, 2, 3, 4]


def test_resync_after_garbage():
    a, b = encode_frame(0, 1, _pcm(320, 1)), encode_frame(1, 2, _pcm(320, 2))
    bad_channel = HEADER.pack(0xCA, 7, 0, 10)
    bad_length = HEADER.pack(0xCA, 0, 0, 10_000_000)
    garbage = b"\x00\x01garbage\xca" + bad_channel + bad_length + b"\xff" * 5
    stats = {}
    got = list(read_frames(io.BytesIO(a + garbage + b), stats=stats))
    assert [(f.channel, f.host_time_ns) for f in got] == [(0, 1), (1, 2)]
    assert stats["resyncs"] > 0
    assert stats["skipped_bytes"] == len(garbage)


def test_truncated_tail():
    a, b = encode_frame(0, 1, _pcm(320, 1)), encode_frame(1, 2, _pcm(320, 2))
    stats = {}
    assert len(list(read_frames(io.BytesIO(a + b[:100]), stats=stats))) == 1
    assert stats["truncated_tail"] is True
    stats = {}
    assert len(list(read_frames(io.BytesIO(a + b[:5]), stats=stats))) == 1
    assert stats["truncated_tail"] is True
    assert list(read_frames(io.BytesIO(b""))) == []
