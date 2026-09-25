import os
from datetime import datetime, timezone

import numpy as np
import shutil

import pytest
import soundfile as sf

from salescoach import config, repo
from salescoach.sources.audio_file import import_audio
from salescoach.store import stores

SRC_RATE = 44100
MTIME = 1_780_000_000


pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

def _tone(seconds, freq, amp):
    t = np.arange(int(seconds * SRC_RATE)) / SRC_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SALES_DB", str(tmp_path / "sales.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    conn = stores.sales()
    yield conn
    conn.close()


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def test_stereo_import(conn, tmp_path):
    wav = tmp_path / "call.wav"
    sf.write(wav, np.stack([_tone(2.0, 440, 0.5), _tone(2.0, 880, 0.05)], axis=1), SRC_RATE)
    os.utime(wav, (MTIME, MTIME))
    call_id = import_audio(conn, wav, "Imported call", lang_mode="en")
    row = repo.get_call(conn, call_id)
    audio_dir = config.calls_dir() / call_id
    assert row["source"] == "audio_file" and row["wf_state"] == "captured" and row["lang_mode"] == "en"
    assert row["audio_dir"] == str(audio_dir)
    assert row["started_at"] == datetime.fromtimestamp(MTIME, timezone.utc).isoformat(timespec="seconds")
    assert row["ended_at"] > row["started_at"]
    me, sr = sf.read(audio_dir / "me.flac", dtype="float32")
    them, _ = sf.read(audio_dir / "them.flac", dtype="float32")
    assert sr == 16000 and me.ndim == 1 and len(me) == len(them)
    assert len(me) / sr == pytest.approx(2.0, abs=0.01)
    assert _rms(me) > 5 * _rms(them) > 0                        # left channel went to me
    events = conn.execute("SELECT type, dedupe_key FROM wf_events WHERE entity_id=?", (call_id,)).fetchall()
    assert [(e["type"], e["dedupe_key"]) for e in events] == [("CALL_ENDED", f"CALL_ENDED:{call_id}")]
    assert not list(config.calls_dir().glob(".import-*"))

    assert import_audio(conn, wav, "same file again") == call_id  # content-hash dedupe
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1


def test_mono_import(conn, tmp_path):
    wav = tmp_path / "mixed.wav"
    sf.write(wav, _tone(1.5, 440, 0.3), SRC_RATE)
    call_id = import_audio(conn, wav, "Mixed", layout="mono_them")
    audio_dir = config.calls_dir() / call_id
    me, _ = sf.read(audio_dir / "me.flac", dtype="int16")
    them, _ = sf.read(audio_dir / "them.flac", dtype="int16")
    assert len(me) == len(them) and len(them) == pytest.approx(1.5 * 16000, abs=20)
    assert not me.any() and np.abs(them).max() > 5000


def test_stereo_layout_rejects_mono(conn, tmp_path):
    wav = tmp_path / "mono.wav"
    sf.write(wav, _tone(0.5, 440, 0.3), SRC_RATE)
    with pytest.raises(ValueError, match="mono_them"):
        import_audio(conn, wav, "wrong layout")
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    assert not list(config.calls_dir().glob(".import-*"))


# ---- security review: only real recordings reach ffmpeg, and only as themselves ----------------------------

def test_a_playlist_naming_another_calls_audio_is_refused_before_ffmpeg(conn, tmp_path, monkeypatch):
    """An HLS playlist (or a concat list) makes a demuxer open OTHER local files: an upload naming
    ../calls/<id>/them.flac imported that call's audio as the uploader's. The content is sniffed first."""
    from salescoach.sources import audio_file
    wav = tmp_path / "secret.wav"
    sf.write(wav, np.stack([_tone(2.0, 300, 0.3), _tone(2.0, 700, 0.3)], axis=1), SRC_RATE)
    victim = import_audio(conn, wav, "someone's call")
    ran = []
    monkeypatch.setattr(audio_file, "_ffmpeg", lambda args: ran.append(args))
    monkeypatch.setattr(audio_file.subprocess, "run", lambda *a, **k: ran.append(a))
    for name, body in (("notes.m3u8", f"#EXTM3U\n#EXTINF:2,\n../calls/{victim}/them.flac\n#EXT-X-ENDLIST\n"),
                       ("list.wav", f"ffconcat version 1.0\nfile '../calls/{victim}/them.flac'\n"),
                       ("empty.mp3", "")):
        path = tmp_path / name
        path.write_text(body)
        with pytest.raises(ValueError, match="not a recording"):
            import_audio(conn, path, "harmless", layout="mono_them")
    assert ran == [] and conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1


def test_ffmpeg_is_told_the_format_and_may_open_only_the_file(conn, tmp_path, monkeypatch):
    from salescoach.sources import audio_file
    wav = tmp_path / "call.wav"
    sf.write(wav, np.stack([_tone(1.0, 440, 0.3), _tone(1.0, 880, 0.3)], axis=1), SRC_RATE)
    seen = []
    real_run = audio_file.subprocess.run
    monkeypatch.setattr(audio_file.subprocess, "run", lambda cmd, **k: (seen.append(cmd), real_run(cmd, **k))[1])
    import_audio(conn, wav, "ok")
    assert len(seen) == 2                                             # ffprobe, then ffmpeg
    for cmd in seen:
        i = cmd.index("-i")
        assert cmd[i - 4:i] == ["-protocol_whitelist", "file", "-f", "wav"] and cmd[i + 1] == f"file:{wav}"
