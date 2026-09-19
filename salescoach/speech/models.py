import os
"""ASR model registry (config/asr.yaml) and the transcriber implementations.

No implicit downloads. mlx_whisper.transcribe() calls snapshot_download() on
any repo id it is given, so a missing model would silently pull gigabytes in
the middle of a call. Here the repo is resolved against the local Hugging
Face cache with local_files_only=True, and mlx_whisper only ever receives the
resulting local directory. A model that is not cached raises ModelNotAvailable
with the command that fetches it; pull() is that command's implementation and
is only ever called explicitly.

MLX inference is serialized by one module-level lock: live and final tiers can
run in different threads, and two concurrent model calls would contend for the
same GPU memory (and mlx_whisper keeps a single global model holder).
"""
import threading
from pathlib import Path
from typing import Callable, Optional, Union

import numpy as np

from .. import config
from ..providers.base import Segment

INFERENCE_LOCK = threading.Lock()
WEIGHT_FILES = ("weights.safetensors", "weights.npz")
TIERS = ("live", "final")


class ModelNotAvailable(RuntimeError):
    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(f"{repo} is not downloaded; run: salescoach models pull {repo}")


# ---- registry ---------------------------------------------------------------

def model_for(tier: str, lang_mode: str = "auto", cfg: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """(repo, whisper language code or None for auto-detect)."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}, not {tier!r}")
    cfg = cfg if cfg is not None else config.load("asr")
    section = cfg.get(tier) or {}
    models = section.get("models") or {}
    repo = models.get(lang_mode) or models.get("auto")
    if not repo:
        raise KeyError(f"asr.yaml has no {tier} model for lang_mode {lang_mode!r}")
    language = (section.get("language") or {}).get(lang_mode)
    return repo, language


def required_models(cfg: Optional[dict] = None) -> list[str]:
    cfg = cfg if cfg is not None else config.load("asr")
    repos = []
    for tier in TIERS:
        for repo in ((cfg.get(tier) or {}).get("models") or {}).values():
            if repo and repo not in repos:
                repos.append(repo)
    return repos


def _complete(path: Path) -> bool:
    return (path / "config.json").exists() and any((path / w).exists() for w in WEIGHT_FILES)


def hub_cache_dir(cache_dir=None) -> Path:
    """The Hugging Face hub cache, honouring HF_HUB_CACHE and HF_HOME like the hub library does."""
    if cache_dir:
        return Path(cache_dir).expanduser()
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    home = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface")).expanduser()
    return home / "hub"


def local_path(repo: str, cache_dir=None) -> Optional[Path]:
    """Local directory holding `repo`, or None. Never touches the network.

    Reads the hub cache layout directly (models--org--name/refs/main -> snapshots/<rev>), so the
    check works without the hub library installed: it only comes with the transcription extra.
    """
    direct = Path(repo).expanduser()
    if direct.is_dir():
        return direct if _complete(direct) else None
    folder = hub_cache_dir(cache_dir) / f"models--{repo.replace('/', '--')}"
    snapshots = folder / "snapshots"
    ref = folder / "refs" / "main"
    candidates = []
    try:
        if ref.is_file():
            candidates.append(snapshots / ref.read_text().strip())
        if snapshots.is_dir():
            candidates += sorted(p for p in snapshots.iterdir() if p.is_dir())
    except OSError:
        return None
    for path in candidates:
        if path.is_dir() and _complete(path):
            return path
    return None


def is_downloaded(repo: str, cache_dir=None) -> bool:
    return local_path(repo, cache_dir) is not None


def missing_models(cfg: Optional[dict] = None, cache_dir=None) -> list[str]:
    return [r for r in required_models(cfg) if not is_downloaded(r, cache_dir)]


def ensure_available(repo: str, cache_dir=None) -> Path:
    path = local_path(repo, cache_dir)
    if path is None:
        raise ModelNotAvailable(repo)
    return path


def pull(repo: str, cache_dir=None) -> str:
    """Explicit download, for `salescoach models pull <repo>`. The only
    function in this package allowed to reach the network for weights."""
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_id=repo, cache_dir=cache_dir)


# ---- transcribers -----------------------------------------------------------

def segments_from_result(result: dict) -> list[Segment]:
    out = []
    for s in result.get("segments") or []:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        words = [{"word": (w.get("word") or "").strip(), "start": float(w.get("start", 0.0)),
                  "end": float(w.get("end", 0.0)), "probability": float(w.get("probability", 0.0))}
                 for w in s.get("words") or []]
        out.append(Segment(channel="", t_start=float(s.get("start", 0.0)), t_end=float(s.get("end", 0.0)),
                           text=text, avg_logprob=s.get("avg_logprob"),
                           no_speech_prob=s.get("no_speech_prob"), words=words))
    return out


class MlxWhisperTranscriber:
    def __init__(self, repo: str, language: Optional[str] = None, cache_dir=None,
                 decode_options: Optional[dict] = None):
        self.repo = self.model = repo
        self.language = language
        self.cache_dir = cache_dir
        self.decode_options = dict(decode_options or {})
        self._path: Optional[Path] = None

    def ensure_available(self) -> Path:
        if self._path is None:
            self._path = ensure_available(self.repo, self.cache_dir)
        return self._path

    def transcribe(self, audio: np.ndarray, word_timestamps: bool = False) -> list[Segment]:
        """audio: float32 mono at 16 kHz. Segment.channel is left for the caller."""
        path = self.ensure_available()
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if audio.size == 0:
            return []
        import mlx_whisper
        with INFERENCE_LOCK:
            result = mlx_whisper.transcribe(
                audio, path_or_hf_repo=str(path), language=self.language,
                word_timestamps=word_timestamps, condition_on_previous_text=False,
                verbose=None, **self.decode_options)
        return segments_from_result(result)


ScriptItem = Union[list, Callable]


class FakeTranscriber:
    """Deterministic stand-in for tests.

    script=None: every call returns one segment spanning the audio, text
    "fake <n>". script=list: one entry per call, each a list of Segment or
    dicts (t_start, t_end, text, avg_logprob, no_speech_prob), or a
    callable(audio, word_timestamps, n) -> list; an exhausted script returns
    []. script=callable: called for every call.
    """
    model = "fake"

    def __init__(self, script: Optional[Union[list, Callable]] = None, sample_rate: int = 16000):
        self.script = list(script) if isinstance(script, list) else script
        self.sample_rate = sample_rate
        self.calls: list[dict] = []

    def ensure_available(self):
        return None

    def transcribe(self, audio: np.ndarray, word_timestamps: bool = False) -> list[Segment]:
        with INFERENCE_LOCK:
            n = len(self.calls)
            self.calls.append({"samples": int(len(audio)), "word_timestamps": word_timestamps})
            if self.script is None:
                item = [Segment("", 0.0, len(audio) / self.sample_rate, f"fake {n}", -0.2, 0.01)]
            elif callable(self.script):
                item = self.script(audio, word_timestamps, n)
            elif self.script:
                item = self.script.pop(0)
            else:
                item = []
            if callable(item):
                item = item(audio, word_timestamps, n)
            return [s if isinstance(s, Segment) else Segment(channel="", **s) for s in item or []]


def get_transcriber(tier: str, lang_mode: str = "auto", cfg: Optional[dict] = None,
                    cache_dir=None) -> MlxWhisperTranscriber:
    """Construct (not load) the configured transcriber. Availability is checked
    on first use, or up front with .ensure_available()."""
    repo, language = model_for(tier, lang_mode, cfg)
    return MlxWhisperTranscriber(repo, language=language, cache_dir=cache_dir)
