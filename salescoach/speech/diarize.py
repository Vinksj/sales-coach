"""Speaker diarization of the 'them' channel (sherpa-onnx, offline, CPU).

The mic channel is one person by construction, so only them.flac is
diarized. The resulting clusters (them_1, them_2, ... in order of first
appearance) are attached to 'them' final turns by maximal time overlap, and a
`speakers` row per cluster is where the UI later maps a cluster to a person.
Re-running keeps any person already mapped to a cluster that still exists.

Models are looked up locally and NEVER fetched. Put them in
    <SALESCOACH_DATA>/models/diarization/        (default data/models/diarization/)

  segmentation.onnx  pyannote segmentation 3.0 exported for sherpa-onnx: the
      model.onnx inside
      https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
      (the extracted folder sherpa-onnx-pyannote-segmentation-3-0/model.onnx
      is also recognised)
  embedding.onnx     a speaker-embedding model from
      https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models
      (sic, the release tag is misspelled), e.g.
      3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx, or
      nemo_en_titanet_small.onnx / wespeaker_en_voxceleb_resnet34.onnx for
      English-heavy calls. Files named *eres2net*, *titanet*, *wespeaker*,
      *campplus* are also recognised.

Without both files diarization is skipped: every 'them' turn becomes them_1
and the call still advances to 'diarized', because a single-cluster "them"
is a correct (if coarse) answer and must not block the pipeline.

Does not commit; the caller does.
"""
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np

from .. import config, repo

FALLBACK = "them_1"
SEGMENTATION_NAMES = ("segmentation.onnx", "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
                      "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx")
EMBEDDING_GLOBS = ("embedding.onnx", "*eres2net*.onnx", "*titanet*.onnx", "*wespeaker*.onnx", "*campplus*.onnx")


def models_dir() -> Path:
    return config.DATA_DIR / "models" / "diarization"


# Verified against the GitHub releases API on 2026-09-11.
SEGMENTATION_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
                    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")                  # 6,958,444 bytes
EMBEDDING_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                 "wespeaker_en_voxceleb_resnet34.onnx")                              # 26,534,365 bytes
DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}


def pull_models(directory=None) -> dict:
    """Explicit, user-approved download of the two diarization models.

    Goes through ~/.claude/lib/safefetch (host allowlist, public-IP check,
    per-hop redirect validation, byte cap), never urllib directly.
    """
    import importlib.util
    import io
    import os
    import tarfile

    spec = importlib.util.spec_from_file_location("safefetch", os.path.expanduser("~/.claude/lib/safefetch.py"))
    safefetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(safefetch)
    d = Path(directory) if directory else models_dir()
    d.mkdir(parents=True, exist_ok=True)
    body, _, _ = safefetch.safe_fetch(SEGMENTATION_URL, DOWNLOAD_HOSTS, timeout=120, max_bytes=20_000_000)
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:bz2") as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("/model.onnx"))
        (d / "segmentation.onnx").write_bytes(tar.extractfile(member).read())
    body, _, _ = safefetch.safe_fetch(EMBEDDING_URL, DOWNLOAD_HOSTS, timeout=300, max_bytes=40_000_000)
    (d / "embedding.onnx").write_bytes(body)
    return {"directory": str(d), "segmentation": (d / "segmentation.onnx").stat().st_size,
            "embedding": (d / "embedding.onnx").stat().st_size}


def find_models(directory=None) -> Optional[tuple[Path, Path]]:
    """(segmentation, embedding) model paths, or None if either is missing."""
    d = Path(directory) if directory else models_dir()
    if not d.is_dir():
        return None
    seg = next((d / n for n in SEGMENTATION_NAMES if (d / n).is_file()), None)
    emb = next((p for g in EMBEDDING_GLOBS for p in sorted(d.glob(g)) if p.is_file()), None)
    return (seg, emb) if seg and emb else None


def run_diarization(audio: np.ndarray, sample_rate: int, segmentation: Path, embedding: Path,
                    num_speakers: Optional[int] = None, threshold: float = 0.5,
                    num_threads: int = 4) -> list[tuple[float, float, int]]:
    """[(start_s, end_s, speaker_int)] sorted by start."""
    import sherpa_onnx
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation)),
            num_threads=num_threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=num_threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers or -1, threshold=threshold),
        min_duration_on=0.3, min_duration_off=0.5)
    if not cfg.validate():
        raise RuntimeError(f"invalid diarization config (models: {segmentation}, {embedding})")
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    if sd.sample_rate != sample_rate:
        raise ValueError(f"diarization expects {sd.sample_rate} Hz audio, got {sample_rate} Hz")
    result = sd.process(np.ascontiguousarray(audio, dtype=np.float32)).sort_by_start_time()
    return [(float(s.start), float(s.end), int(s.speaker)) for s in result]


def label_clusters(segments) -> list[tuple[float, float, str]]:
    """Rename speaker ints to them_1, them_2, ... in order of first appearance."""
    names: dict[int, str] = {}
    for _, _, spk in sorted(segments):
        names.setdefault(spk, f"them_{len(names) + 1}")
    return [(s, e, names[spk]) for s, e, spk in segments]


def assign_clusters(turns, segments) -> list[str]:
    """Cluster label per turn: maximal overlap, else the nearest segment."""
    out = []
    for turn in turns:
        t0, t1 = float(turn["t_start"]), float(turn["t_end"])
        if not segments:
            out.append(FALLBACK)
            continue
        overlap: dict[str, float] = defaultdict(float)
        for s, e, label in segments:
            ov = min(t1, e) - max(t0, s)
            if ov > 0:
                overlap[label] += ov
        if overlap:
            out.append(max(overlap, key=overlap.get))
        else:
            out.append(min(segments, key=lambda seg: max(seg[0] - t1, t0 - seg[1], 0.0))[2])
    return out


def diarize_call(conn, call_id: str, models_dir=None, num_speakers: Optional[int] = None,
                 threshold: float = 0.5) -> dict:
    row = repo.get_call(conn, call_id)
    if row is None:
        raise KeyError(call_id)
    turns = conn.execute("SELECT idx, channel, t_start, t_end FROM turns "
                         "WHERE call_id=? AND tier='final' ORDER BY idx", (call_id,)).fetchall()
    them = [t for t in turns if t["channel"] == "them"]
    has_me = any(t["channel"] == "me" for t in turns)
    them_path = Path(row["audio_dir"] or "") / "them.flac"
    result: dict = {}
    labelled: list = []
    found = find_models(models_dir)
    if found is None:
        result["skipped"] = "diarization models not present"
    elif not row["audio_dir"] or not them_path.exists():
        result["skipped"] = "them.flac not found"
    elif not them:
        result["skipped"] = "no 'them' turns"
    else:
        from .final import load_channel
        rate = int(config.load("asr").get("sample_rate", 16000))
        try:
            segments = run_diarization(load_channel(them_path, rate), rate, *found,
                                       num_speakers=num_speakers, threshold=threshold)
            labelled = label_clusters(segments)
            result["segments"] = len(labelled)
        except Exception as exc:                # a broken model file must not block the call
            result["skipped"] = f"diarization failed: {type(exc).__name__}: {exc}"

    labels = assign_clusters(them, labelled) if labelled else [FALLBACK] * len(them)
    for turn, label in zip(them, labels):
        conn.execute("UPDATE turns SET speaker_cluster=? WHERE call_id=? AND tier='final' AND idx=?",
                     (label, call_id, turn["idx"]))
    conn.execute("UPDATE turns SET speaker_cluster='me' WHERE call_id=? AND tier='final' AND channel='me'",
                 (call_id,))

    wanted = {label: "them" for label in labels}
    if has_me:
        wanted["me"] = "me"
    existing = {r["cluster"] for r in conn.execute("SELECT cluster FROM speakers WHERE call_id=?", (call_id,))}
    for cluster in existing - set(wanted):
        conn.execute("DELETE FROM speakers WHERE call_id=? AND cluster=?", (call_id, cluster))
    for cluster, channel in wanted.items():
        conn.execute("INSERT INTO speakers(call_id,cluster,channel) VALUES (?,?,?) "
                     "ON CONFLICT(call_id,cluster) DO UPDATE SET channel=excluded.channel",
                     (call_id, cluster, channel))
    repo.set_call_state(conn, call_id, "diarized")
    result["clusters"] = sorted(c for c in wanted if c != "me")
    result["turns_assigned"] = len(them)
    return result
