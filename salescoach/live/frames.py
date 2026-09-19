"""callcap stdout framing.

    [0xCA][u8 channel][u64 LE host_time_ns][u32 LE n][n x s16le mono]

The reader must never lose sync for good: a torn write or stray bytes (a
library printing to stdout inside callcap) would otherwise turn the rest of
the call into noise. On a header that fails the sanity checks it scans
forward for the next 0xCA and tries again. A truncated tail at EOF is dropped,
because half a frame has no trustworthy length.
"""
import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Optional

import numpy as np

MAGIC = 0xCA
HEADER = struct.Struct("<BBQI")
CHANNELS = {0: "me", 1: "them"}
# callcap frames are one audio callback (tens of ms). Anything claiming more
# than a few seconds is a false header found while resyncing.
MAX_SAMPLES = 16000 * 4


@dataclass
class Frame:
    channel: int
    host_time_ns: int
    pcm: bytes                       # s16le mono

    @property
    def n(self) -> int:
        return len(self.pcm) // 2

    @property
    def name(self) -> str:
        return CHANNELS[self.channel]

    def samples(self) -> np.ndarray:
        return np.frombuffer(self.pcm, dtype="<i2")


def encode_frame(channel: int, host_time_ns: int, pcm) -> bytes:
    if isinstance(pcm, np.ndarray):
        pcm = np.asarray(pcm, dtype="<i2").tobytes()
    return HEADER.pack(MAGIC, channel, host_time_ns, len(pcm) // 2) + pcm


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks, got = [], 0
    while got < size:
        chunk = stream.read(size - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frames(stream: BinaryIO, max_samples: int = MAX_SAMPLES,
                stats: Optional[dict] = None) -> Iterator[Frame]:
    """Yield frames until EOF. `stats` (if given) accumulates resyncs,
    skipped_bytes and truncated_tail."""
    stats = stats if stats is not None else {}
    for key in ("frames", "resyncs", "skipped_bytes"):
        stats.setdefault(key, 0)
    stats.setdefault("truncated_tail", False)
    buf = bytearray()
    while True:
        if len(buf) < HEADER.size:
            buf += _read_exact(stream, HEADER.size - len(buf))
            if len(buf) < HEADER.size:
                if buf:
                    stats["truncated_tail"] = True
                return
        magic, channel, host_time_ns, n = HEADER.unpack_from(buf)
        if magic != MAGIC or channel not in CHANNELS or not 0 < n <= max_samples:
            idx = buf.find(bytes([MAGIC]), 1)
            drop = idx if idx != -1 else len(buf)
            del buf[:drop]
            stats["resyncs"] += 1
            stats["skipped_bytes"] += drop
            continue
        payload = _read_exact(stream, n * 2)
        if len(payload) < n * 2:
            stats["truncated_tail"] = True
            return
        buf.clear()
        stats["frames"] += 1
        yield Frame(channel, host_time_ns, payload)
