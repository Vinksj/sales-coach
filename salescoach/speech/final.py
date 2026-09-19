"""Final-tier transcription of a captured call.

Runs after capture, on the FLAC archive, with the slower and more accurate
final model and word timestamps. Each channel is transcribed on its own: the
channel split (me = mic, them = system audio) is the most reliable speaker
separation there is, so the transcript never has to guess which side said
what.

Before any LLM reads the transcript, every turn is pre-labelled from
Whisper's own confidence (asr.yaml quality.*), and 'me' turns that repeat the
other side are flagged as bleed. The Quality Assessor refines these labels
rather than having to discover them.

Long audio is cut into windows of at most 30 minutes, at the quietest point
near each boundary, so one model call never holds an hour of audio and a cut
avoids landing mid-word where it can.

Does not commit: the workflow step commits together with
TRANSCRIPT_FINALIZED. All transcription finishes before the first write, so
the write transaction stays short.
"""
import hashlib
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

from .. import config, repo
from ..live import archive
from ..live.echo import flag_bleed
from ..providers.base import Segment
from . import models

CHANNELS = ("me", "them")
WINDOW_S = 1800.0
SEAM_SEARCH_S = 10.0
SILENT_PEAK = 1e-4               # a channel that never exceeds this has nothing to transcribe


def load_channel(path, sample_rate: int = 16000) -> np.ndarray:
    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if rate != sample_rate:
        raise ValueError(f"{path} is {rate} Hz; expected {sample_rate} Hz")
    return audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]


def windows(audio: np.ndarray, sample_rate: int = 16000, window_s: float = WINDOW_S,
            search_s: float = SEAM_SEARCH_S) -> list[tuple[int, int]]:
    """[(start, end)] sample ranges, each at most window_s long."""
    n, win = len(audio), int(window_s * sample_rate)
    if n <= win:
        return [(0, n)]
    block = max(1, sample_rate // 10)
    bounds = [0]
    while n - bounds[-1] > win:
        hi = bounds[-1] + win
        lo = max(bounds[-1] + win // 2, hi - int(search_s * sample_rate))
        seg = audio[lo:hi]
        k = len(seg) // block
        if k:
            energy = np.square(seg[: k * block].reshape(k, block)).mean(axis=1)
            cut = lo + int(np.argmin(energy)) * block + block // 2
        else:
            cut = hi
        bounds.append(cut)
    bounds.append(n)
    return list(zip(bounds[:-1], bounds[1:]))


def transcribe_channel(transcriber, audio: np.ndarray, channel: str, sample_rate: int = 16000,
                       window_s: float = WINDOW_S, word_timestamps: bool = True) -> list[Segment]:
    out = []
    for a, b in windows(audio, sample_rate, window_s):
        offset = a / sample_rate
        for s in transcriber.transcribe(audio[a:b], word_timestamps=word_timestamps):
            text = (s.text or "").strip()
            if not text:
                continue
            out.append(Segment(channel, round(offset + s.t_start, 3), round(offset + s.t_end, 3), text,
                               s.avg_logprob, s.no_speech_prob,
                               [{**w, "start": offset + w.get("start", 0.0), "end": offset + w.get("end", 0.0)}
                                for w in s.words]))
    return out


def assess(avg_logprob: Optional[float], no_speech_prob: Optional[float],
           thresholds: dict) -> tuple[Optional[str], Optional[str]]:
    """(quality, quality_note) from ASR confidence alone."""
    if avg_logprob is None and no_speech_prob is None:
        return None, None
    garbled = thresholds.get("logprob_garbled", -1.2)
    partial = thresholds.get("logprob_partial", -0.8)
    no_speech = thresholds.get("no_speech_garbled", 0.6)
    if (avg_logprob is not None and avg_logprob < garbled) or \
            (no_speech_prob is not None and no_speech_prob > no_speech):
        quality = "garbled"
    elif avg_logprob is not None and avg_logprob < partial:
        quality = "partial"
    else:
        quality = "ok"
    return quality, "asr_confidence"


def transcript_sha(turns) -> str:
    """sha256 over "channel|t_start|text" lines: the identity of a transcript
    that downstream steps key their idempotency on."""
    lines = [f"{t['channel']}|{float(t['t_start']):.2f}|{t['text']}" for t in turns]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def transcribe_call(conn, call_id: str, transcriber_factory: Optional[Callable[[str], object]] = None,
                    window_s: Optional[float] = None) -> int:
    """Replace the call's final turns with a fresh final-tier transcript.
    Returns the number of turns. The caller commits."""
    row = repo.get_call(conn, call_id)
    if row is None:
        raise KeyError(call_id)
    if row["wf_state"] == "live":
        raise RuntimeError(f"call {call_id} is still live; stop it before final transcription")
    if not row["audio_dir"]:
        raise FileNotFoundError(f"call {call_id} has no audio_dir")
    audio_dir = Path(row["audio_dir"])
    archive.recover(audio_dir)                  # a crash between capture and finalize leaves .pcm
    cfg = config.load("asr")
    rate = int(cfg.get("sample_rate", 16000))
    final_cfg = cfg.get("final") or {}
    word_timestamps = bool(final_cfg.get("word_timestamps", True))
    window_s = window_s or float(final_cfg.get("window_s", WINDOW_S))
    factory = transcriber_factory or (lambda lang_mode: models.get_transcriber("final", lang_mode, cfg))
    transcriber = factory(row["lang_mode"] or "auto")
    ensure = getattr(transcriber, "ensure_available", None)
    if ensure:
        ensure()                                # fail before touching the existing transcript
    paths = {ch: audio_dir / f"{ch}.flac" for ch in CHANNELS}
    if not any(p.exists() for p in paths.values()):
        raise FileNotFoundError(f"no me.flac / them.flac in {audio_dir}")

    segments: list[Segment] = []
    for channel, path in paths.items():
        if not path.exists():
            continue
        audio = load_channel(path, rate)
        if audio.size == 0 or float(np.max(np.abs(audio))) < SILENT_PEAK:
            continue
        segments += transcribe_channel(transcriber, audio, channel, rate, window_s, word_timestamps)
    segments.sort(key=lambda s: (s.t_start, s.t_end, CHANNELS.index(s.channel)))

    thresholds = cfg.get("quality") or {}
    turns = []
    for s in segments:
        quality, note = assess(s.avg_logprob, s.no_speech_prob, thresholds)
        turns.append({"channel": s.channel, "t_start": s.t_start, "t_end": s.t_end, "text": s.text,
                      "asr_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
                      "quality": quality, "quality_note": note, "bleed_flag": 0})
    flag_bleed(turns)
    sha = transcript_sha(turns)

    conn.execute("DELETE FROM turns WHERE call_id=? AND tier='final'", (call_id,))
    conn.executemany(
        "INSERT INTO turns(call_id,tier,idx,channel,t_start,t_end,text,asr_logprob,no_speech_prob,"
        "quality,quality_note,bleed_flag) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(call_id, "final", i, t["channel"], t["t_start"], t["t_end"], t["text"], t["asr_logprob"],
          t["no_speech_prob"], t["quality"], t["quality_note"], t["bleed_flag"]) for i, t in enumerate(turns)])
    repo.update_call(conn, call_id, asr_final_model=getattr(transcriber, "model", None),
                     transcript_sha=sha, wf_state="final_transcribed")
    return len(turns)
