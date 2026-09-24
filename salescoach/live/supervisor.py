"""One live capture: callcap process -> archive, levels, VAD, live ASR, turns.

Threads, each owning exactly one thing:
  reader   callcap stdout -> frames -> archive (the only archive writer), level
           meters, per-channel VAD -> LiveTranscriber queue
  stderr   callcap JSON status lines -> hub "status"; error/fatal -> "alert"
  monitor  every tick: publish levels, fire silent-channel alerts, fsync the
           archive, notice callcap dying on its own
  live-asr (LiveTranscriber) transcribes and persists live turns through its
           own sales.db handle (sqlite handles are per-thread)

The order of priorities is fixed: audio on disk first, alerts second, live
transcript last. A callcap that dies early, a model that is not downloaded, a
locked database: each is reported on the hub and in stop()'s stats, and none
stops the others.
"""
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from .. import config
from ..providers.base import Segment
from ..store import stores
from ..store import db
from . import hub as hub_module
from . import stream_asr
from .archive import NAMES, Archive
from .frames import read_frames
from .levels import Levels
from .vad import EnergyVAD

MAX_ALERTS_KEPT = 50


def _kill_group(proc) -> None:
    """SIGKILL the capture helper and every process it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


class CaptureSession:
    def __init__(self, call_id: str, audio_dir, lang_mode: str = "auto",
                 binary: Union[None, str, Path, Sequence[str]] = None, transcriber=None,
                 callcap_args: Sequence[str] = (), hub: Optional[hub_module.Hub] = None,
                 transcriber_factory: Optional[Callable[[str], object]] = None,
                 db_path=None, cfg: Optional[dict] = None, tick_s: float = 0.25):
        self.call_id = call_id
        self.audio_dir = Path(audio_dir)
        self.lang_mode = lang_mode
        self.binary = binary
        self.transcriber = transcriber
        self.transcriber_factory = transcriber_factory
        self.callcap_args = list(callcap_args)
        self.hub = hub or hub_module.hub
        self.topic = hub_module.topic_for(call_id)
        self.db_path = db_path
        self.cfg = cfg if cfg is not None else config.load("asr")
        self.rate = int(self.cfg.get("sample_rate", 16000))
        self.tick_s = tick_s
        self.proc: Optional[subprocess.Popen] = None
        self.archive: Optional[Archive] = None
        self.live: Optional[stream_asr.LiveTranscriber] = None
        self.asr_model: Optional[str] = None
        self.frame_stats: dict = {}
        self.last_heartbeat: Optional[dict] = None
        self.alerts: list[dict] = []
        self.turns = 0
        self.died_early = False
        self._next_idx = 0
        self._stopping = False
        self._stopped: Optional[dict] = None
        self._stop_evt = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._db = threading.local()
        self._archive_error_at = 0.0
        self.started_at: Optional[float] = None

    # ---- lifecycle ---------------------------------------------------------

    def command(self) -> list[str]:
        cap = self.cfg.get("capture") or {}
        binary = self.binary or cap.get("binary", "callcap/build/callcap")
        if isinstance(binary, (list, tuple)):
            cmd = [str(part) for part in binary]
        else:
            path = Path(binary)
            if not path.is_absolute():
                path = config.ROOT / path
            if not path.exists():
                raise FileNotFoundError(f"callcap binary not found at {path}; build it with callcap/build.sh")
            cmd = [str(path)]
        cmd += ["--sample-rate", str(self.rate)]
        if not cap.get("voice_processing", True):
            cmd.append("--no-vp")
        return cmd + self.callcap_args

    def start(self) -> None:
        cmd = self.command()
        cap = self.cfg.get("capture") or {}
        self.archive = Archive(self.audio_dir, sample_rate=self.rate)
        self.levels = Levels(self.rate, silence_alert_s=cap.get("silence_alert_seconds", 60))
        self.vads = {name: EnergyVAD.from_config(name, self.cfg) for name in NAMES}
        conn = stores.sales(self.db_path)
        try:
            row = conn.execute("SELECT MAX(idx) FROM turns WHERE call_id=? AND tier='live'",
                               (self.call_id,)).fetchone()
            self._next_idx = (row[0] + 1) if row[0] is not None else 0
        finally:
            conn.close()
        self.live = self._make_live()
        # Own session: callcap re-spawns itself (see main.swift), and a SIGKILL of the launcher alone
        # would orphan the capturing child with the mic still hot.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
        self.started_at = time.time()
        for name, target in (("reader", self._read_loop), ("stderr", self._stderr_loop),
                             ("monitor", self._monitor_loop)):
            thread = threading.Thread(target=target, name=f"capture-{name}", daemon=True)
            self._threads[name] = thread
            thread.start()
        self._publish({"type": "status", "event": "session_started", "pid": self.proc.pid,
                       "asr_model": self.asr_model, "asr_enabled": self.live is not None})

    def _make_live(self) -> Optional[stream_asr.LiveTranscriber]:
        try:
            transcriber = self.transcriber
            if transcriber is None and self.transcriber_factory is not None:
                transcriber = self.transcriber_factory(self.lang_mode)
            live = stream_asr.create(self.lang_mode, self._on_segment, transcriber=transcriber, cfg=self.cfg,
                                     on_error=self._on_asr_error, on_exit=self._close_thread_db)
            live.check()
        except Exception as exc:                    # capture never depends on ASR
            self._alert("asr_unavailable", f"live transcript disabled: {exc}")
            return None
        self.asr_model = live.model
        return live.start()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 5.0, drain_timeout: float = 120.0) -> dict:
        """SIGTERM callcap, drain everything, finalize the archive. Idempotent."""
        if self._stopped is not None:
            return self._stopped
        self._stopping = True
        returncode = None
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout)
                except subprocess.TimeoutExpired:
                    _kill_group(self.proc)
                    self.proc.wait(5.0)
            returncode = self.proc.returncode
        for name in ("reader", "stderr"):
            thread = self._threads.get(name)
            if thread:
                thread.join(30.0)
        self._stop_evt.set()
        if self._threads.get("monitor"):
            self._threads["monitor"].join(5.0)
        asr = self.live.stop(drain=True, timeout=drain_timeout) if self.live else {}
        final = self.archive.finalize() if self.archive else {}
        stats = {
            "call_id": self.call_id,
            "returncode": returncode,
            "died_early": self.died_early,
            "frames": self.frame_stats.get("frames", 0),
            "resyncs": self.frame_stats.get("resyncs", 0),
            "skipped_bytes": self.frame_stats.get("skipped_bytes", 0),
            "truncated_tail": self.frame_stats.get("truncated_tail", False),
            "samples": dict(self.archive.written) if self.archive else {},
            "gaps": self.archive.stats["gaps"] if self.archive else 0,
            "duration_s": round(final.get("duration_s", 0.0), 3),
            "files": final.get("files", {}),
            "turns": self.turns,
            "asr_model": self.asr_model,
            "asr": asr,
            "alerts": len(self.alerts),
            "elapsed_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
        }
        self._publish({"type": "status", "event": "session_stopped", **{k: stats[k] for k in (
            "returncode", "died_early", "duration_s", "turns")}})
        self._stopped = stats
        return stats

    def status(self) -> dict:
        return {
            "alive": self.alive,
            "died_early": self.died_early,
            "levels": self.levels.snapshot() if hasattr(self, "levels") else {},
            "heartbeat": self.last_heartbeat,
            "duration_s": round(self.archive.duration_s(), 1) if self.archive else 0.0,
            "turns": self.turns,
            "asr": {"model": self.asr_model, "enabled": self.live is not None,
                    "disabled": self.live.disabled if self.live else "unavailable",
                    "backlog": self.live.backlog if self.live else 0},
            "alerts": self.alerts[-10:],
        }

    # ---- threads -----------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for frame in read_frames(self.proc.stdout, stats=self.frame_stats):
                pcm = frame.samples()
                start = None
                try:
                    start = self.archive.write(frame)
                except Exception as exc:
                    if time.monotonic() - self._archive_error_at > 10:
                        self._archive_error_at = time.monotonic()
                        self._alert("archive_error", f"audio not saved: {type(exc).__name__}: {exc}")
                self.levels.update(frame.name, pcm)
                if self.live is not None:
                    for seg in self.vads[frame.name].feed(pcm, start):
                        self.live.submit(seg)
        except Exception as exc:
            self._alert("reader_error", f"{type(exc).__name__}: {exc}")
        finally:
            if self.live is not None:
                for vad in self.vads.values():
                    for seg in vad.flush():
                        self.live.submit(seg)

    def _stderr_loop(self) -> None:
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    raise ValueError
            except ValueError:
                msg = {"event": "log", "message": line}
            event = msg.get("event", "log")
            if event == "heartbeat":
                self.last_heartbeat = msg
            self._publish({**msg, "type": "status", "event": event})
            if event in ("error", "fatal"):
                self._alert(f"capture_{event}", msg.get("message", ""), source=msg.get("source"))

    def _monitor_loop(self) -> None:
        while not self._stop_evt.wait(self.tick_s):
            self._publish({"type": "level", **self.levels.snapshot(),
                           "t": round(self.archive.duration_s(), 2)})
            for channel in self.levels.check_silence():
                self._alert("silence", f"no audio on '{channel}' for {self.levels.silence_alert_s:.0f}s",
                            channel=channel)
            self.archive.maybe_sync()
            if not self._stopping and not self.died_early and self.proc.poll() is not None:
                self.died_early = True
                self._alert("capture_died", f"callcap exited on its own (code {self.proc.returncode}); "
                            "audio up to this point is saved", returncode=self.proc.returncode)

    # ---- sinks -------------------------------------------------------------

    def _thread_db(self) -> db.Connection:
        conn = getattr(self._db, "conn", None)
        if conn is None:
            conn = self._db.conn = stores.sales(self.db_path)
        return conn

    def _close_thread_db(self) -> None:
        conn = getattr(self._db, "conn", None)
        if conn is not None:
            conn.close()
            self._db.conn = None

    def _on_segment(self, seg: Segment) -> None:
        idx = self._next_idx
        self._next_idx += 1
        conn = self._thread_db()
        try:
            conn.execute(
                "INSERT INTO turns(call_id,tier,idx,channel,t_start,t_end,text,asr_logprob,no_speech_prob) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (self.call_id, "live", idx, seg.channel, seg.t_start, seg.t_end, seg.text,
                 seg.avg_logprob, seg.no_speech_prob))
            conn.commit()
            self.turns += 1
        except db.Error as exc:
            conn.rollback()
            self._alert("db_error", f"live turn not saved: {exc}")
        self._publish({"type": "segment", "idx": idx, "channel": seg.channel, "t_start": seg.t_start,
                       "t_end": seg.t_end, "text": seg.text, "avg_logprob": seg.avg_logprob,
                       "no_speech_prob": seg.no_speech_prob})

    def _on_asr_error(self, kind: str, message: str) -> None:
        self._alert(kind, message)

    def _alert(self, kind: str, message: str, **extra) -> None:
        alert = {"type": "alert", "kind": kind, "message": message, "ts": time.time(),
                 **{k: v for k, v in extra.items() if v is not None}}
        self.alerts.append(alert)
        del self.alerts[:-MAX_ALERTS_KEPT]
        self._publish(alert)

    def _publish(self, message: dict) -> None:
        self.hub.publish(self.topic, message)
