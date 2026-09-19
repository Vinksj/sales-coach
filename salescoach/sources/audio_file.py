"""Import any recording as a call.

A recording enters the same pipeline as a live capture: it becomes me.flac /
them.flac (16 kHz mono s16 FLAC) in the call's audio_dir, the call is created
at 'captured', and CALL_ENDED hands it to the workflow.

Layouts:
  stereo_me_left  left channel = me, right = them (a dual-track recorder).
  mono_them       one mixed track. It all goes to them.flac and me.flac is
                  silence of the same length. Diarization then has to separate
                  the voices, because the channel split carries no speaker
                  information.

Re-importing the same file (same content hash) returns the existing call
instead of creating a duplicate.

ffmpeg does the decoding, so any container works (m4a, mp3, wav, webm...).
Conversion finishes before the database transaction opens, so a long file
never holds the sales.db write lock.
"""
import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import soundfile as sf

from .. import config, repo
from ..live.archive import write_silent_flac
from ..orchestrator import bus
from ..schemas.events import Event
from ..store import stores

LAYOUTS = ("stereo_me_left", "mono_them")


def _tool(name: str) -> str:
    path = shutil.which(name) or f"/opt/homebrew/bin/{name}"
    if not Path(path).exists():
        raise FileNotFoundError(f"{name} not found; install it with: brew install ffmpeg")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_channels(path) -> int:
    proc = subprocess.run([_tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, timeout=120)
    value = proc.stdout.strip().splitlines()[0].strip(",") if proc.stdout.strip() else ""
    if proc.returncode != 0 or not value.isdigit():
        raise ValueError(f"no audio stream in {path}: {proc.stderr.strip()[:300]}")
    return int(value)


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run([_tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-500:]}")


def convert(path, out_dir, layout: str, sample_rate: int = 16000) -> float:
    """Write me.flac / them.flac into out_dir; returns the duration in seconds."""
    out_dir = Path(out_dir)
    me, them = out_dir / "me.flac", out_dir / "them.flac"
    enc = ["-ar", str(sample_rate), "-sample_fmt", "s16", "-c:a", "flac"]
    if layout == "stereo_me_left":
        channels = probe_channels(path)
        if channels < 2:
            raise ValueError(f"{path} has {channels} channel; use layout='mono_them'")
        _ffmpeg(["-i", str(path), "-filter_complex",
                 "[0:a:0]asplit=2[l][r];[l]pan=mono|c0=c0[me];[r]pan=mono|c0=c1[them]",
                 "-map", "[me]", *enc, str(me), "-map", "[them]", *enc, str(them)])
    elif layout == "mono_them":
        _ffmpeg(["-i", str(path), "-map", "0:a:0", "-ac", "1", *enc, str(them)])
        write_silent_flac(me, sample_rate, sf.info(str(them)).frames)
    else:
        raise ValueError(f"layout must be one of {LAYOUTS}, not {layout!r}")
    info = sf.info(str(them))
    return info.frames / info.samplerate


def import_audio(conn, path, title: str, deal_id: Optional[str] = None, lang_mode: str = "auto",
                 layout: str = "stereo_me_left") -> str:
    """Create a 'captured' call from a recording and publish CALL_ENDED. Commits."""
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}, not {layout!r}")
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
    sha = _sha256(src)
    source_ref = f"audio_file:{sha[:32]}"
    existing = conn.execute("SELECT node_id FROM calls WHERE source_ref=?", (source_ref,)).fetchone()
    if existing:
        return existing["node_id"]

    rate = int(config.load("asr").get("sample_rate", 16000))
    calls = config.calls_dir()
    tmp = calls / f".import-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        duration = convert(src, tmp, layout, rate)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    started = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc)
    started_at = started.isoformat(timespec="seconds")
    ended_at = (started + timedelta(seconds=duration)).isoformat(timespec="seconds")
    audio_dir: Optional[Path] = None
    try:
        call_id = repo.create_call(conn, source="audio_file", title=title, deal_id=deal_id, lang_mode=lang_mode,
                                   started_at=started_at, source_ref=source_ref, wf_state="captured")
        audio_dir = calls / call_id
        tmp.rename(audio_dir)
        (audio_dir / "meta.json").write_text(json.dumps(
            {"sample_rate": rate, "finalized": True, "duration_s": round(duration, 3), "source": "audio_file",
             "file": src.name, "layout": layout, "sha256": sha}, indent=2, sort_keys=True))
        repo.update_call(conn, call_id, audio_dir=str(audio_dir), ended_at=ended_at)
        stores.engine.set_source(conn, repo.ACTOR, call_id, uri=src.as_uri(), sha=sha, raw_path=str(audio_dir),
                                 capture="audio_file", lineage=[{"file": src.name, "layout": layout}])
        bus.publish(conn, Event(type="CALL_ENDED", entity_id=call_id, dedupe_key=f"CALL_ENDED:{call_id}",
                                payload={"call_id": call_id, "audio_dir": str(audio_dir),
                                         "duration_s": round(duration, 3), "source": "audio_file"}))
        conn.commit()
    except BaseException:
        conn.rollback()
        shutil.rmtree(audio_dir if audio_dir is not None and audio_dir.exists() else tmp, ignore_errors=True)
        raise
    return call_id
