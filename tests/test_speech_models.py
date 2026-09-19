"""Model registry: no implicit downloads, local paths only."""
import socket
import types

import numpy as np
import pytest

from salescoach.speech import models
from salescoach.speech.models import (FakeTranscriber, MlxWhisperTranscriber, ModelNotAvailable,
                                      get_transcriber, local_path, model_for)

CFG = {
    "live": {"models": {"auto": "org/live-model"}, "language": {"auto": None}},
    "final": {"models": {"en": "org/final-en", "auto": "org/final-auto"},
              "language": {"en": "en", "hinglish": "hi", "auto": None}},
}


@pytest.fixture
def no_network(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


def _fake_cache(root, repo="org/tiny", revision="abc123"):
    folder = root / f"models--{repo.replace('/', '--')}"
    snapshot = folder / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "weights.safetensors").write_bytes(b"")
    (folder / "refs").mkdir()
    (folder / "refs" / "main").write_text(revision)
    return snapshot


def test_model_for():
    assert model_for("final", "en", CFG) == ("org/final-en", "en")
    assert model_for("final", "hinglish", CFG) == ("org/final-auto", "hi")   # falls back to auto's repo
    assert model_for("live", "en", CFG) == ("org/live-model", None)
    assert models.required_models(CFG) == ["org/live-model", "org/final-en", "org/final-auto"]
    with pytest.raises(ValueError):
        model_for("medium", "en", CFG)


def test_real_config_models_resolve():
    for tier in models.TIERS:
        for mode in ("en", "hinglish", "auto"):
            repo, _ = model_for(tier, mode)
            assert repo.startswith("mlx-community/")


def test_missing_model_raises_without_network(tmp_path, no_network):
    transcriber = get_transcriber("final", "en", CFG, cache_dir=tmp_path)
    with pytest.raises(ModelNotAvailable, match="run: salescoach models pull org/final-en"):
        transcriber.transcribe(np.zeros(16000, dtype=np.float32))
    assert models.missing_models(CFG, cache_dir=tmp_path) == models.required_models(CFG)
    assert no_network == []


def test_cached_model_passes_local_path(tmp_path, monkeypatch, no_network):
    snapshot = _fake_cache(tmp_path)
    assert local_path("org/tiny", cache_dir=tmp_path) == snapshot
    seen = {}

    def fake_transcribe(audio, **kwargs):
        seen.update(kwargs, locked=models.INFERENCE_LOCK.locked(), dtype=audio.dtype)
        return {"segments": [
            {"start": 0.0, "end": 1.0, "text": " hi there ", "avg_logprob": -0.1, "no_speech_prob": 0.02,
             "words": [{"word": " hi", "start": 0.1, "end": 0.4, "probability": 0.9}]},
            {"start": 1.0, "end": 1.5, "text": "  "}]}

    monkeypatch.setitem(__import__("sys").modules, "mlx_whisper",
                        types.SimpleNamespace(transcribe=fake_transcribe))
    segs = MlxWhisperTranscriber("org/tiny", language="en", cache_dir=tmp_path).transcribe(
        np.zeros(16000, dtype=np.float64), word_timestamps=True)
    assert seen["path_or_hf_repo"] == str(snapshot)          # never a repo id
    assert seen["condition_on_previous_text"] is False and seen["language"] == "en"
    assert seen["word_timestamps"] is True and seen["locked"] and seen["dtype"] == np.float32
    assert len(segs) == 1 and segs[0].text == "hi there" and segs[0].channel == ""
    assert segs[0].avg_logprob == -0.1 and segs[0].words[0] == {"word": "hi", "start": 0.1, "end": 0.4,
                                                                "probability": 0.9}
    assert no_network == []


def test_local_directory_and_incomplete_download(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    assert local_path(str(tmp_path)) is None                  # no weights yet
    (tmp_path / "weights.npz").write_bytes(b"")
    assert local_path(str(tmp_path)) == tmp_path


def test_fake_transcriber_script():
    fake = FakeTranscriber([[{"t_start": 0.0, "t_end": 1.0, "text": "a"}], lambda audio, wt, n: []])
    assert [s.text for s in fake.transcribe(np.zeros(10))] == ["a"]
    assert fake.transcribe(np.zeros(10)) == [] and fake.transcribe(np.zeros(10)) == []
    assert FakeTranscriber().transcribe(np.zeros(8000))[0].t_end == 0.5
