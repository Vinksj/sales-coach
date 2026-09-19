"""Per-channel level meters and the silent-channel alarm.

The failure this guards against is capture that looks fine and records
nothing: a muted mic, a tap on the wrong device, a callcap that stopped
sending one channel. Silence is measured on the wall clock, not on audio time,
so a channel that stops producing frames altogether counts as silent too.
"""
import math
import threading
import time
from collections import deque

import numpy as np

FLOOR_DBFS = -120.0


def dbfs(value: float) -> float:
    return FLOOR_DBFS if value <= 1e-6 else max(FLOOR_DBFS, 20.0 * math.log10(value))


class Levels:
    def __init__(self, sample_rate: int = 16000, silence_alert_s: float = 60.0,
                 window_s: float = 0.3, silence_dbfs: float = -50.0, stale_s: float = 1.0,
                 channels=("me", "them"), clock=time.monotonic):
        self.sample_rate = sample_rate
        self.silence_alert_s = silence_alert_s
        self.window = int(window_s * sample_rate)
        self.silence_dbfs = silence_dbfs
        self.stale_s = stale_s
        self.clock = clock
        self._lock = threading.Lock()
        start = clock()
        self._blocks = {ch: deque() for ch in channels}       # (sumsq, n, peak)
        self._n = {ch: 0 for ch in channels}
        self._last_frame = {ch: None for ch in channels}
        self._last_sound = {ch: start for ch in channels}
        self._alerted = {ch: False for ch in channels}

    def update(self, channel: str, pcm: np.ndarray) -> None:
        if pcm.size == 0:
            return
        x = pcm.astype(np.float32) / 32768.0
        sumsq = float(np.dot(x, x))
        peak = float(np.max(np.abs(x)))
        now = self.clock()
        with self._lock:
            blocks = self._blocks[channel]
            blocks.append((sumsq, x.size, peak))
            self._n[channel] += x.size
            while blocks and self._n[channel] - blocks[0][1] >= self.window:
                self._n[channel] -= blocks.popleft()[1]
            self._last_frame[channel] = now
            if dbfs(math.sqrt(sumsq / x.size)) > self.silence_dbfs:
                self._last_sound[channel] = now
                self._alerted[channel] = False

    def snapshot(self) -> dict:
        now = self.clock()
        out = {}
        with self._lock:
            for ch, blocks in self._blocks.items():
                last = self._last_frame[ch]
                if not blocks or last is None or now - last > self.stale_s:
                    rms = peak = 0.0
                else:
                    total = sum(b[1] for b in blocks)
                    rms = math.sqrt(sum(b[0] for b in blocks) / total)
                    peak = max(b[2] for b in blocks)
                out[ch] = {
                    "rms_dbfs": round(dbfs(rms), 1),
                    "peak_dbfs": round(dbfs(peak), 1),
                    "silent_s": round(now - self._last_sound[ch], 1),
                    "receiving": last is not None and now - last <= self.stale_s,
                }
        return out

    def check_silence(self) -> list[str]:
        """Channels that have just crossed the silence threshold. Edge-triggered:
        a channel alerts once, then re-arms when it produces sound again."""
        if not self.silence_alert_s:
            return []
        now = self.clock()
        fired = []
        with self._lock:
            for ch in self._blocks:
                if not self._alerted[ch] and now - self._last_sound[ch] >= self.silence_alert_s:
                    self._alerted[ch] = True
                    fired.append(ch)
        return fired
