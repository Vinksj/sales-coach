"""End-to-end capture with a fake callcap (never the real binary)."""
import queue
import sys
import time

import numpy as np
import pytest
import soundfile as sf

from salescoach import config, repo
from salescoach.live.archive import Archive
from salescoach.live.frames import Frame
from salescoach.live.hub import Hub, topic_for
from salescoach.live.manager import LiveCallActive, LiveManager
from salescoach.live.supervisor import CaptureSession
from salescoach.speech.models import FakeTranscriber
from salescoach.store import stores

FAKE_CALLCAP = r'''
import json, signal, struct, sys, time
import numpy as np

rate, frame = 16000, 320
hold = "--hold" in sys.argv
stop = False

def on_term(signum, _frame):
    global stop
    stop = True

signal.signal(signal.SIGTERM, on_term)

def status(**fields):
    fields.setdefault("ts", time.time())
    sys.stderr.write(json.dumps(fields) + "\n")
    sys.stderr.flush()

status(event="starting", sample_rate=rate)
status(event="mic_started")
status(event="system_started")
sys.stderr.write("a plain log line\n")
status(event="error", source="mic", message="voice processing unavailable, fell back")

t = np.arange(int(6.0 * rate)) / rate
rng = np.random.default_rng(0)

def channel(on, off):
    x = rng.normal(0, 8, t.size)
    mask = (t >= on) & (t < off)
    x[mask] += 0.3 * 32767 * np.sin(2 * np.pi * 440 * t[mask])
    return np.clip(x, -32768, 32767).astype("<i2")

me, them = channel(1.0, 3.0), channel(3.5, 5.5)
out = sys.stdout.buffer
base = 10_000_000_000
for i in range(0, t.size, frame):
    ts = base + int(i * 1e9 / rate)
    for ch, data in ((0, me), (1, them)):
        chunk = data[i:i + frame]
        out.write(struct.pack("<BBQI", 0xCA, ch, ts, len(chunk)) + chunk.tobytes())
    if (i // frame) % 100 == 0:
        status(event="heartbeat", samples_me=i, samples_them=i, peak_me=9830, peak_them=9830)
out.flush()
if hold:
    deadline = time.time() + 30
    while not stop and time.time() < deadline:
        time.sleep(0.02)
status(event="stopped", samples_me=int(t.size), samples_them=int(t.size))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SALES_DB", str(tmp_path / "sales.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return tmp_path


@pytest.fixture
def fake_callcap(tmp_path):
    script = tmp_path / "fake_callcap.py"
    script.write_text(FAKE_CALLCAP)
    return [sys.executable, str(script)]


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_supervisor_end_to_end_and_early_exit(env, fake_callcap):
    conn = stores.sales()
    audio_dir = config.calls_dir() / "session"
    call_id = repo.create_call(conn, source="capture", title="t", audio_dir=str(audio_dir))
    conn.commit()
    hub = Hub()
    q = hub.subscribe(topic_for(call_id), maxsize=100000)
    fake = FakeTranscriber()
    session = CaptureSession(call_id, audio_dir, binary=fake_callcap, transcriber=fake, hub=hub, tick_s=0.05)
    session.start()
    assert _wait_for(lambda: session.died_early)         # callcap exited on its own: reported, not fatal
    stats = session.stop()
    assert session.stop() is stats                       # idempotent

    assert stats["returncode"] == 0 and stats["died_early"] is True
    assert stats["frames"] == 600 and stats["resyncs"] == 0
    assert stats["duration_s"] == pytest.approx(6.0) and stats["turns"] == 2
    rows = repo.turns(conn, call_id, "live")
    assert [(r["idx"], r["channel"]) for r in rows] == [(0, "me"), (1, "them")]
    assert rows[0]["t_start"] == pytest.approx(0.8, abs=0.1) and rows[0]["t_end"] == pytest.approx(3.2, abs=0.1)
    assert rows[1]["t_start"] == pytest.approx(3.3, abs=0.1) and rows[1]["t_end"] == pytest.approx(5.7, abs=0.1)
    assert rows[0]["text"] == "fake 0" and rows[0]["asr_logprob"] == pytest.approx(-0.2)

    messages = _drain(q)
    types = {m["type"] for m in messages}
    assert {"segment", "status", "level", "alert"} <= types
    events = {m.get("event") for m in messages if m["type"] == "status"}
    assert {"starting", "heartbeat", "log", "stopped", "session_started", "session_stopped"} <= events
    kinds = {m["kind"] for m in messages if m["type"] == "alert"}
    assert {"capture_error", "capture_died"} <= kinds
    segs = [m for m in messages if m["type"] == "segment"]
    assert [m["channel"] for m in segs] == ["me", "them"]

    me, sr = sf.read(audio_dir / "me.flac", dtype="int16")
    them, _ = sf.read(audio_dir / "them.flac", dtype="int16")
    assert sr == 16000 and len(me) == len(them) == 96000
    assert np.abs(me[16000:48000]).max() > 5000 and np.abs(me[56000:88000]).max() < 5000
    assert not list(audio_dir.glob("*.pcm"))
    conn.close()


def test_manager_start_stop_and_dedupe(env, fake_callcap):
    conn = stores.sales()
    person = repo.create_person(conn, "Prospect", email="p@example.com")
    conn.commit()
    hub = Hub()
    mgr = LiveManager(hub=hub, binary=fake_callcap, callcap_args=["--hold"],
                      transcriber_factory=lambda lang_mode: FakeTranscriber())
    call_id = mgr.start_call("Discovery", lang_mode="en", participants=[person])
    q = hub.subscribe(topic_for(call_id), maxsize=100000)
    status = mgr.status()
    assert status["active"] and status["call_id"] == call_id and status["asr"]["model"] == "fake"
    with pytest.raises(LiveCallActive):
        mgr.start_call("second call")
    row = repo.get_call(conn, call_id)
    assert row["wf_state"] == "live" and row["asr_live_model"] == "fake"
    assert row["audio_dir"] == str(config.calls_dir() / call_id)

    assert _wait_for(lambda: session_frames(mgr) >= 600)
    stats = mgr.stop_call(call_id)
    assert stats["call_ended_published"] is True and stats["died_early"] is False
    assert stats["returncode"] == 0 and stats["turns"] == 2
    row = repo.get_call(conn, call_id)
    assert row["wf_state"] == "captured" and row["ended_at"]
    assert [r["type"] for r in conn.execute(
        "SELECT type FROM wf_events WHERE entity_id=? ORDER BY id", (call_id,))] == ["CALL_STARTED", "CALL_ENDED"]
    assert [p["node_id"] for p in repo.call_participants(conn, call_id)] == [person]
    assert any(m["type"] == "ended" for m in _drain(q))
    assert mgr.status() == {"active": False}

    again = mgr.stop_call(call_id)
    assert again["call_ended_published"] is False
    assert conn.execute("SELECT COUNT(*) FROM wf_events WHERE type='CALL_ENDED'").fetchone()[0] == 1
    conn.close()


def session_frames(mgr):
    session = mgr._session
    return session.frame_stats.get("frames", 0) if session else 0


def test_manager_missing_binary_marks_capture_failed(env):
    mgr = LiveManager(hub=Hub(), binary=str(env / "no-such-callcap"))
    with pytest.raises(FileNotFoundError, match="callcap binary not found"):
        mgr.start_call("broken")
    conn = stores.sales()
    row = conn.execute("SELECT * FROM calls").fetchone()
    assert row["wf_state"] == "capture_failed" and "callcap binary not found" in row["wf_error"]
    assert conn.execute("SELECT COUNT(*) FROM wf_events").fetchone()[0] == 0
    assert mgr.status() == {"active": False}
    assert mgr.stop_call(row["node_id"]) == {"call_id": row["node_id"], "skipped": "capture_failed"}
    conn.close()


def test_recover_orphans_after_crash(env):
    conn = stores.sales()
    audio_dir = config.calls_dir() / "crashed"
    call_id = repo.create_call(conn, source="capture", title="crashed", audio_dir=str(audio_dir), wf_state="live")
    conn.commit()
    archive = Archive(audio_dir, sample_rate=16000, fsync_interval_s=0)
    pcm = np.full(16000, 1000, dtype="<i2").tobytes()
    archive.write(Frame(0, 1_000, pcm))
    archive.write(Frame(1, 1_000, pcm))
    for fh in archive._files.values():                  # the process died here
        fh.close()

    mgr = LiveManager(hub=Hub())
    assert mgr.recover_orphans() == [call_id]
    row = repo.get_call(conn, call_id)
    assert row["wf_state"] == "captured" and row["ended_at"]
    assert (audio_dir / "me.flac").exists() and not (audio_dir / "me.pcm").exists()
    assert conn.execute("SELECT COUNT(*) FROM wf_events WHERE type='CALL_ENDED'").fetchone()[0] == 1
    assert mgr.recover_orphans() == []
    conn.close()
