"""Live transcription: VAD segments from both channels -> Segments on the call timeline.

One worker thread serves both channels, because MLX inference is serialized
anyway (speech.models.INFERENCE_LOCK) and a second thread would only queue on
the lock. The queue is bounded: if the model falls behind real time the
OLDEST pending segment is dropped and reported. The live view gets a gap;
the final tier re-transcribes the whole archive, so nothing is lost for good.

A model that is not downloaded disables live ASR with one alert. Capture
carries on.

Engines (asr.yaml live.engine):
  chunked         VAD-segmented mlx-whisper (this module). Default.
  whisperlivekit  not wired yet: the adapter lands after the S2 model bake-off.
"""
import queue
import threading
from typing import Callable, Optional

from ..providers.base import Segment
from ..speech.models import ModelNotAvailable, get_transcriber
from .vad import SpeechSegment

_STOP = object()


class LiveTranscriber:
    def __init__(self, transcriber, on_segment: Callable[[Segment], None], max_queue: int = 32,
                 on_error: Optional[Callable[[str, str], None]] = None,
                 on_exit: Optional[Callable[[], None]] = None, word_timestamps: bool = False):
        self.transcriber = transcriber
        self.model = getattr(transcriber, "model", None)
        self.on_segment = on_segment
        self.on_error = on_error or (lambda kind, message: None)
        self.on_exit = on_exit
        self.word_timestamps = word_timestamps
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._abort = threading.Event()
        self.disabled: Optional[str] = None
        self.stats = {"submitted": 0, "transcribed": 0, "segments": 0, "dropped": 0, "errors": 0,
                      "skipped": 0, "abandoned": 0}

    def check(self) -> None:
        """Raise ModelNotAvailable now rather than on the first utterance."""
        ensure = getattr(self.transcriber, "ensure_available", None)
        if ensure:
            ensure()

    def start(self) -> "LiveTranscriber":
        self._thread = threading.Thread(target=self._run, name="live-asr", daemon=True)
        self._thread.start()
        return self

    @property
    def backlog(self) -> int:
        return self._q.qsize()

    def submit(self, speech: SpeechSegment) -> None:
        self.stats["submitted"] += 1
        while True:
            try:
                self._q.put_nowait(speech)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                    self.stats["dropped"] += 1
                    if self.stats["dropped"] in (1, 10) or self.stats["dropped"] % 50 == 0:
                        self.on_error("asr_backlog",
                                      f"live ASR is behind real time; {self.stats['dropped']} segment(s) "
                                      "skipped (the final transcript will include them)")
                except queue.Empty:
                    pass

    def stop(self, drain: bool = True, timeout: Optional[float] = None) -> dict:
        """Finish queued work (drain=True) or discard it, then stop the worker."""
        if self._thread is None:
            return dict(self.stats)
        if not drain:
            self._discard()
        self._q.put(_STOP)
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._abort.set()
            self._thread.join(5.0)
        self._thread = None
        return dict(self.stats)

    def _discard(self) -> None:
        while True:
            try:
                self._q.get_nowait()
                self.stats["abandoned"] += 1
            except queue.Empty:
                return

    def _run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    return
                if self._abort.is_set():
                    self.stats["abandoned"] += 1
                    continue
                if self.disabled:
                    self.stats["skipped"] += 1
                    continue
                self._transcribe(item)
        finally:
            if self.on_exit:
                self.on_exit()

    def _transcribe(self, speech: SpeechSegment) -> None:
        try:
            segments = self.transcriber.transcribe(speech.float32(), word_timestamps=self.word_timestamps)
        except ModelNotAvailable as exc:
            self.disabled = str(exc)
            self.on_error("asr_unavailable", str(exc))
            return
        except Exception as exc:                        # one bad segment must not end live ASR
            self.stats["errors"] += 1
            self.on_error("asr_error", f"{type(exc).__name__}: {exc}")
            return
        self.stats["transcribed"] += 1
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            t_start = speech.t_start + max(0.0, seg.t_start)
            t_end = min(speech.t_end, speech.t_start + max(seg.t_end, seg.t_start))
            out = Segment(channel=speech.channel, t_start=round(t_start, 3), t_end=round(max(t_end, t_start), 3),
                          text=text, avg_logprob=seg.avg_logprob, no_speech_prob=seg.no_speech_prob,
                          words=[{**w, "start": speech.t_start + w.get("start", 0.0),
                                  "end": speech.t_start + w.get("end", 0.0)} for w in seg.words])
            self.stats["segments"] += 1
            try:
                self.on_segment(out)
            except Exception as exc:
                self.on_error("sink_error", f"{type(exc).__name__}: {exc}")


def create(lang_mode: str, on_segment: Callable[[Segment], None], transcriber=None,
           cfg: Optional[dict] = None, **kwargs) -> LiveTranscriber:
    """Build the configured live engine. Raises NotImplementedError for an
    engine that is not wired yet, ModelNotAvailable is raised lazily."""
    if cfg is None:
        from .. import config
        cfg = config.load("asr")
    engine = (cfg.get("live") or {}).get("engine", "chunked")
    if engine == "whisperlivekit":
        raise NotImplementedError(
            "live.engine 'whisperlivekit' is not wired yet: the adapter lands after the S2 model "
            "bake-off (docs/asr-bakeoff.md). Set live.engine: chunked in config/asr.yaml.")
    if engine != "chunked":
        raise ValueError(f"unknown live.engine {engine!r} (expected chunked | whisperlivekit)")
    if transcriber is None:
        transcriber = get_transcriber("live", lang_mode, cfg)
    return LiveTranscriber(transcriber, on_segment, **kwargs)
