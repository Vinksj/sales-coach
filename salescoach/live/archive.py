"""Crash-safe per-channel audio archive.

While a call is live, each channel is appended as raw s16le to me.pcm /
them.pcm and fsynced at least every fsync_interval_s. Raw PCM rather than
streaming FLAC because an append-only headerless file is readable up to its
last flushed byte after any crash; a FLAC killed mid-stream is not. A crash
loses at most the unflushed seconds, never the call. finalize() (or
recover() after a crash) converts to me.flac / them.flac.

Alignment: t0 is the first host_time_ns seen on either channel and is
persisted in meta.json. Every frame is placed at (host_time - t0) * rate;
if a channel has fallen more than 50 ms behind that position (a dropout, a
channel that started late) the hole is filled with zeros. So sample k of
either file is always k / rate seconds after t0, and a turn's t_start plays
the same moment on both channels. Frames arriving slightly early are appended
as-is: audio is never dropped to fix timing.
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

from .frames import CHANNELS, Frame

NAMES = ("me", "them")
META = "meta.json"
GAP_S = 0.05
MAX_GAP_S = 6 * 3600          # beyond this the clock is wrong, not the audio
CHUNK = 1 << 20               # samples per read/write block when converting


def read_meta(audio_dir) -> dict:
    path = Path(audio_dir) / META
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_meta(audio_dir: Path, meta: dict) -> None:
    path = audio_dir / META
    tmp = path.with_name(f".{META}.tmp")
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_zeros(fh, n: int) -> None:
    block = np.zeros(min(n, CHUNK), dtype="<i2").tobytes()
    while n > 0:
        k = min(n, CHUNK)
        fh.write(block[: k * 2])
        n -= k


class Archive:
    def __init__(self, audio_dir, sample_rate: Optional[int] = None,
                 fsync_interval_s: float = 10.0, clock=time.monotonic):
        self.dir = Path(audio_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta = read_meta(self.dir)
        if self._meta.get("finalized"):
            raise RuntimeError(f"archive in {self.dir} is already finalized")
        if sample_rate is None and "sample_rate" not in self._meta:
            from .. import config
            sample_rate = config.load("asr").get("sample_rate", 16000)
        self.rate = int(self._meta.get("sample_rate") or sample_rate)
        self.t0_ns: Optional[int] = self._meta.get("t0_ns")
        self.gap = int(GAP_S * self.rate)
        self.fsync_interval_s = fsync_interval_s
        self.clock = clock
        self._lock = threading.Lock()
        self._files: dict = {}
        self._closed = False
        self._last_sync = clock()
        # resuming an interrupted call in the same dir continues both timelines
        self.written = {name: self._pcm(name).stat().st_size // 2 if self._pcm(name).exists() else 0
                        for name in NAMES}
        self.stats = {"frames": 0, "gaps": 0, "padded_samples": {n: 0 for n in NAMES},
                      "clock_anomalies": 0, "syncs": 0}

    def _pcm(self, name: str) -> Path:
        return self.dir / f"{name}.pcm"

    def _file(self, name: str):
        fh = self._files.get(name)
        if fh is None:
            path = self._pcm(name)
            if path.exists() and path.stat().st_size % 2:
                os.truncate(path, path.stat().st_size - 1)      # torn last sample
            fh = open(path, "ab")
            self._files[name] = fh
        return fh

    def write(self, frame: Frame) -> int:
        """Append a frame; returns the absolute sample index its audio starts at."""
        name = CHANNELS[frame.channel]
        with self._lock:
            if self._closed:
                raise RuntimeError("archive is closed")
            if self.t0_ns is None:
                self.t0_ns = frame.host_time_ns
                self._meta.update(t0_ns=self.t0_ns, t0_wall=time.time(), sample_rate=self.rate,
                                  channels=list(NAMES), version=1)
                _write_meta(self.dir, self._meta)
            fh = self._file(name)
            pos = self.written[name]
            expected = round((frame.host_time_ns - self.t0_ns) * self.rate / 1e9)
            gap = expected - pos
            if gap > self.gap:
                if gap > MAX_GAP_S * self.rate:
                    self.stats["clock_anomalies"] += 1
                else:
                    _write_zeros(fh, gap)
                    self.stats["gaps"] += 1
                    self.stats["padded_samples"][name] += gap
                    pos = expected
            fh.write(frame.pcm)
            self.written[name] = pos + frame.n
            self.stats["frames"] += 1
            self._maybe_sync_locked()
            return pos

    def position(self, name: str) -> int:
        return self.written[name]

    def duration_s(self) -> float:
        return max(self.written.values()) / self.rate

    def _sync_locked(self) -> None:
        for fh in self._files.values():
            fh.flush()
            os.fsync(fh.fileno())
        self._last_sync = self.clock()
        self.stats["syncs"] += 1

    def _maybe_sync_locked(self) -> None:
        if self.clock() - self._last_sync >= self.fsync_interval_s:
            self._sync_locked()

    def maybe_sync(self) -> None:
        """Called from the monitor tick so a channel that stops sending frames
        still gets its tail on disk."""
        with self._lock:
            if not self._closed:
                self._maybe_sync_locked()

    def sync(self) -> None:
        with self._lock:
            if not self._closed:
                self._sync_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._sync_locked()
            for fh in self._files.values():
                fh.close()
            self._files.clear()
            self._closed = True

    def finalize(self) -> dict:
        self.close()
        return finalize_dir(self.dir, self.rate)


def _pcm_to_flac(pcm: Path, flac: Path, rate: int, target: int) -> None:
    tmp = flac.with_name(f".{flac.name}.tmp")
    n = pcm.stat().st_size // 2
    with open(pcm, "rb") as src, sf.SoundFile(str(tmp), "w", samplerate=rate, channels=1,
                                               subtype="PCM_16", format="FLAC") as dst:
        remaining = n
        while remaining > 0:
            k = min(CHUNK, remaining)
            dst.write(np.frombuffer(src.read(k * 2), dtype="<i2"))
            remaining -= k
        _write_silence(dst, target - n)
    os.replace(tmp, flac)


def _write_silence(dst, n: int) -> None:
    while n > 0:
        k = min(CHUNK, n)
        dst.write(np.zeros(k, dtype=np.int16))
        n -= k


def write_silent_flac(flac: Path, rate: int, n: int) -> None:
    tmp = flac.with_name(f".{flac.name}.tmp")
    with sf.SoundFile(str(tmp), "w", samplerate=rate, channels=1, subtype="PCM_16", format="FLAC") as dst:
        _write_silence(dst, n)
    os.replace(tmp, flac)


def finalize_dir(audio_dir, sample_rate: Optional[int] = None) -> dict:
    """Convert leftover .pcm to .flac, padding both channels to equal length.

    Idempotent and restartable: a channel already converted keeps its FLAC,
    and a .pcm is removed only after its FLAC is complete."""
    d = Path(audio_dir)
    meta = read_meta(d)
    rate = int(meta.get("sample_rate") or sample_rate or 16000)
    lengths = {}
    for name in NAMES:
        pcm, flac = d / f"{name}.pcm", d / f"{name}.flac"
        if pcm.exists():
            lengths[name] = pcm.stat().st_size // 2
        elif flac.exists():
            lengths[name] = sf.info(str(flac)).frames
    target = max(lengths.values(), default=0)
    if target == 0:
        for name in NAMES:
            (d / f"{name}.pcm").unlink(missing_ok=True)
        return {"files": {}, "duration_s": 0.0, "samples": lengths, "sample_rate": rate}
    files = {}
    for name in NAMES:
        pcm, flac = d / f"{name}.pcm", d / f"{name}.flac"
        if pcm.exists():
            _pcm_to_flac(pcm, flac, rate, target)
            pcm.unlink()
        elif not flac.exists():
            write_silent_flac(flac, rate, target)
        files[name] = str(flac)
    meta.update(finalized=True, duration_s=round(target / rate, 3), samples=lengths, sample_rate=rate,
                finalized_at=time.time())
    _write_meta(d, meta)
    return {"files": files, "duration_s": target / rate, "samples": lengths, "sample_rate": rate}


def recover(audio_dir) -> Optional[dict]:
    """After a crash: finalize any leftover .pcm. None when there is nothing to do."""
    d = Path(audio_dir)
    if not any((d / f"{name}.pcm").exists() for name in NAMES):
        return None
    return finalize_dir(d)
