"""Energy VAD with an adaptive noise floor, one instance per channel.

Why energy and not Silero/WebRTC: the two channels are already separated at
the source (voice-processed mic, process tap), so the job is only "where does
someone start and stop talking", and a model would be one more download that
live capture depends on. Whisper's own no_speech_prob filters what slips
through.

Segmenting rules (config/asr.yaml live.*):
  - speech  = block energy above max(noise floor + margin, absolute minimum).
  - a pause of silence_s closes a segment, but only once it holds at least
    min_segment_s of audio; a shorter one stays open so "yes... so the
    pricing" reaches Whisper as one utterance instead of a hallucination-prone
    half second. A short segment still closes after short_flush_s of silence,
    so a lone "okay" is not lost.
  - a segment reaching max_segment_s is split at its quietest recent block.
  - segments with less than min_speech_s of speech are clicks; dropped.

Positions are absolute sample indices on the archive's shared timeline, so a
segment's times are seconds since t0 and match the FLAC exactly.
"""
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .levels import FLOOR_DBFS, dbfs


@dataclass
class SpeechSegment:
    channel: str
    start: int                       # absolute sample index
    end: int
    audio: np.ndarray                # int16, len == end - start
    sample_rate: int = 16000

    @property
    def t_start(self) -> float:
        return self.start / self.sample_rate

    @property
    def t_end(self) -> float:
        return self.end / self.sample_rate

    @property
    def duration(self) -> float:
        return (self.end - self.start) / self.sample_rate

    def float32(self) -> np.ndarray:
        return self.audio.astype(np.float32) / 32768.0


@dataclass
class _Block:
    start: int
    samples: np.ndarray
    db: float
    speech: bool

    @property
    def end(self) -> int:
        return self.start + len(self.samples)


class EnergyVAD:
    def __init__(self, channel: str, sample_rate: int = 16000, min_segment_s: float = 1.2,
                 max_segment_s: float = 12.0, silence_s: float = 0.6, block_s: float = 0.03,
                 margin_db: float = 9.0, abs_min_db: float = -55.0, floor_init_db: float = -65.0,
                 floor_min_db: float = -75.0, floor_max_db: float = -35.0, onset_blocks: int = 2,
                 preroll_s: float = 0.2, tail_s: float = 0.2, min_speech_s: float = 0.25,
                 short_flush_s: float = 1.5, split_search_s: float = 3.0):
        self.channel = channel
        self.rate = sample_rate
        self.block = max(1, int(block_s * sample_rate))
        self.min_segment = int(min_segment_s * sample_rate)
        self.max_segment = int(max_segment_s * sample_rate)
        self.silence = int(silence_s * sample_rate)
        self.short_flush = int(max(short_flush_s, silence_s) * sample_rate)
        self.tail = int(tail_s * sample_rate)
        self.min_speech = int(min_speech_s * sample_rate)
        self.split_search = int(split_search_s * sample_rate)
        self.margin_db = margin_db
        self.abs_min_db = abs_min_db
        self.floor_min_db = floor_min_db
        self.floor_max_db = floor_max_db
        self.floor_db = floor_init_db
        self.floor_init_db = floor_init_db
        self.onset_blocks = onset_blocks
        preroll_blocks = int(round(preroll_s * sample_rate / self.block))
        self._ring: deque[_Block] = deque(maxlen=preroll_blocks + onset_blocks)
        self._carry = np.zeros(0, dtype=np.int16)
        self._carry_start = 0
        self._onset = 0
        self._seg: Optional[list[_Block]] = None
        self._last_speech_end = 0
        self._prev_seg_end = 0

    @classmethod
    def from_config(cls, channel: str, cfg: Optional[dict] = None, **overrides) -> "EnergyVAD":
        if cfg is None:
            from .. import config
            cfg = config.load("asr")
        live = cfg.get("live", {})
        kwargs = dict(sample_rate=cfg.get("sample_rate", 16000),
                      min_segment_s=live.get("min_segment_s", 1.2),
                      max_segment_s=live.get("max_segment_s", 12.0),
                      silence_s=live.get("silence_s", 0.6))
        kwargs.update(overrides)
        return cls(channel, **kwargs)

    @property
    def position(self) -> int:
        return self._carry_start + len(self._carry)

    # ---- input -------------------------------------------------------------

    def feed(self, pcm: np.ndarray, start: Optional[int] = None) -> list[SpeechSegment]:
        """Consume int16 samples. `start` is their absolute position; a jump
        forward (an archive gap) is treated as silence."""
        out: list[SpeechSegment] = []
        if start is not None and start > self.position:
            out += self._gap(start - self.position)
        data = np.concatenate([self._carry, np.asarray(pcm, dtype=np.int16)])
        pos = self._carry_start
        usable = len(data) - len(data) % self.block
        for i in range(0, usable, self.block):
            out += self._process(_block_at(pos + i, data[i:i + self.block]))
        self._carry = data[usable:].copy()
        self._carry_start = pos + usable
        return out

    def flush(self) -> list[SpeechSegment]:
        """End of stream: close whatever is open."""
        out: list[SpeechSegment] = []
        if len(self._carry):
            out += self._process(_block_at(self._carry_start, self._carry))
            self._carry_start += len(self._carry)
            self._carry = np.zeros(0, dtype=np.int16)
        if self._seg is not None:
            out += self._close(self._seg[-1].end)
        return out

    def _gap(self, n: int) -> list[SpeechSegment]:
        out: list[SpeechSegment] = []
        if len(self._carry):
            fill = self.block - len(self._carry)
            take = min(fill, n)
            out += self.feed(np.zeros(take, dtype=np.int16))
            n -= take
        if n <= 0:
            return out
        if self._seg is not None:
            silent_now = self._seg[-1].end - self._last_speech_end
            if silent_now + n < self.short_flush:
                # a short dropout inside speech: keep timing exact by feeding zeros
                return out + self.feed(np.zeros(n, dtype=np.int16))
            out += self._close(self._seg[-1].end)
        self._ring.clear()
        self._onset = 0
        self._carry_start += n
        return out

    # ---- per block ---------------------------------------------------------

    def _process(self, blk: _Block) -> list[SpeechSegment]:
        blk.speech = blk.db > max(self.floor_db + self.margin_db, self.abs_min_db)
        self._track_floor(blk)
        self._ring.append(blk)
        if self._seg is None:
            self._onset = self._onset + 1 if blk.speech else 0
            if self._onset >= self.onset_blocks:
                self._seg = [b for b in self._ring if b.start >= self._prev_seg_end]
                self._last_speech_end = blk.end
                self._onset = 0
            return []
        self._seg.append(blk)
        if blk.speech:
            self._last_speech_end = blk.end
        seg_start = self._seg[0].start
        silent = blk.end - self._last_speech_end
        spoken = self._last_speech_end - seg_start
        if silent >= self.silence and (spoken >= self.min_segment or silent >= self.short_flush):
            return self._close(blk.end)
        if blk.end - seg_start >= self.max_segment:
            return self._split()
        return []

    def _track_floor(self, blk: _Block) -> None:
        if blk.db <= FLOOR_DBFS:
            # Digital silence says nothing about room noise, but a floor driven up by steady loud audio
            # must not stay stuck there: relax it toward the initial estimate so quieter speech that
            # follows is still segmented (gated call apps emit exact zeros between utterances).
            if self.floor_db > self.floor_init_db:
                self.floor_db += (self.floor_init_db - self.floor_db) * 0.2
            return
        if blk.db < self.floor_db:
            rate = 0.2
        elif not blk.speech:
            rate = 0.01
        else:
            rate = 0.002
        self.floor_db += (blk.db - self.floor_db) * rate
        self.floor_db = min(self.floor_max_db, max(self.floor_min_db, self.floor_db))

    def _close(self, at: int) -> list[SpeechSegment]:
        blocks, self._seg = self._seg or [], None
        if not blocks:
            return []
        start = blocks[0].start
        end = min(at, self._last_speech_end + self.tail)
        self._prev_seg_end = end
        speech = sum(len(b.samples) for b in blocks if b.speech)
        if speech < self.min_speech or end <= start:
            return []
        audio = np.concatenate([b.samples for b in blocks])[: end - start]
        return [SpeechSegment(self.channel, start, start + len(audio), audio, self.rate)]

    def _split(self) -> list[SpeechSegment]:
        blocks = self._seg
        horizon = blocks[-1].end - self.split_search
        candidates = [i for i, b in enumerate(blocks) if b.start >= horizon and i < len(blocks) - 1]
        cut = min(candidates, key=lambda i: blocks[i].db) if candidates else len(blocks) - 1
        head, rest = blocks[: cut + 1], blocks[cut + 1:]
        start, end = head[0].start, head[-1].end
        audio = np.concatenate([b.samples for b in head])
        out = []
        if sum(len(b.samples) for b in head if b.speech) >= self.min_speech:
            out.append(SpeechSegment(self.channel, start, end, audio, self.rate))
        self._prev_seg_end = end
        if rest and any(b.speech for b in rest):
            self._seg = rest
            self._last_speech_end = max(b.end for b in rest if b.speech)
        else:
            self._seg = None
        return out


def _block_at(start: int, samples: np.ndarray) -> _Block:
    x = samples.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.dot(x, x) / len(x))) if len(x) else 0.0
    return _Block(start, samples, dbfs(rms), False)
