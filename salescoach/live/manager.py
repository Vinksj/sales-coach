"""Process-wide live-call manager: one capture at a time.

It owns the transitions the rest of the system cares about:
  start_call  creates the call row (wf_state 'live') and publishes
              CALL_STARTED only once callcap is actually running. A capture
              that cannot start leaves the row at 'capture_failed' with the
              error, so a failed attempt is visible rather than a phantom
              live call.
  stop_call   stops the session, sets ended_at and wf_state 'captured', and
              publishes CALL_ENDED (dedupe_key CALL_ENDED:<id>) in the same
              commit. Calling it again is safe: the event dedupes and the
              call's later state is never rewound.
  recover_orphans  after a crash, calls left at 'live' get their archive
              finalized and are ended the same way.

Web handlers call these from arbitrary threads, so every method opens its own
sales.db handle.
"""
import threading
import time
from typing import Callable, Optional, Sequence

from .. import config, repo
from ..orchestrator import bus
from ..schemas.events import Event
from ..store import stores
from . import archive
from . import hub as hub_module
from .supervisor import CaptureSession


class LiveCallActive(RuntimeError):
    pass


class LiveManager:
    def __init__(self, hub: Optional[hub_module.Hub] = None, binary=None,
                 transcriber_factory: Optional[Callable[[str], object]] = None,
                 callcap_args: Sequence[str] = (), db_path=None):
        self.hub = hub or hub_module.hub
        self.binary = binary
        self.transcriber_factory = transcriber_factory
        self.callcap_args = tuple(callcap_args)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._session: Optional[CaptureSession] = None
        self._call: dict = {}

    def _conn(self):
        return stores.sales(self.db_path)

    @property
    def active_call_id(self) -> Optional[str]:
        return self._call.get("call_id") if self._session else None

    def start_call(self, title: str, deal_id: Optional[str] = None, lang_mode: str = "auto",
                   participants: Sequence[str] = ()) -> str:
        with self._lock:
            if self._session is not None:
                raise LiveCallActive(f"a call is already live: {self._call['call_id']}")
            conn = self._conn()
            try:
                call_id = repo.create_call(conn, source="capture", title=title, deal_id=deal_id,
                                           lang_mode=lang_mode, wf_state="live")
                audio_dir = config.calls_dir() / call_id
                audio_dir.mkdir(parents=True, exist_ok=True)
                repo.update_call(conn, call_id, audio_dir=str(audio_dir))
                stores.engine.set_source(conn, repo.ACTOR, call_id, uri=f"capture:{call_id}",
                                         capture="capture", raw_path=str(audio_dir))
                for person_id in participants:
                    repo.add_participant(conn, call_id, person_id)
                conn.commit()
                session = CaptureSession(call_id, audio_dir, lang_mode, binary=self.binary,
                                         transcriber_factory=self.transcriber_factory,
                                         callcap_args=self.callcap_args, hub=self.hub, db_path=self.db_path)
                try:
                    session.start()
                except Exception as exc:
                    repo.set_call_state(conn, call_id, "capture_failed", error=f"{type(exc).__name__}: {exc}"[:500])
                    conn.commit()
                    raise
                if session.asr_model:
                    repo.update_call(conn, call_id, asr_live_model=session.asr_model)
                started_at = repo.get_call(conn, call_id)["started_at"]
                bus.publish(conn, Event(type="CALL_STARTED", entity_id=call_id,
                                        dedupe_key=f"CALL_STARTED:{call_id}",
                                        payload={"call_id": call_id, "title": title, "deal_id": deal_id,
                                                 "lang_mode": lang_mode, "audio_dir": str(audio_dir)}))
                conn.commit()
            finally:
                conn.close()
            self._session = session
            self._call = {"call_id": call_id, "title": title, "deal_id": deal_id, "lang_mode": lang_mode,
                          "started_at": started_at, "t0": time.monotonic()}
            self.hub.publish(hub_module.topic_for(call_id), {"type": "status", "event": "call_started",
                                                             "call_id": call_id, "title": title})
            return call_id

    def stop_call(self, call_id: Optional[str] = None) -> dict:
        with self._lock:
            session = None
            if self._session is not None and call_id in (None, self._call["call_id"]):
                session, call_id = self._session, self._call["call_id"]
            if call_id is None:
                raise LookupError("no live call to stop")
            if session is not None:
                try:
                    stats = session.stop()
                finally:
                    self._session, self._call = None, {}
            conn = self._conn()
            try:
                row = repo.get_call(conn, call_id)
                if row is None:
                    raise KeyError(call_id)
                if row["wf_state"] == "capture_failed":
                    return {"call_id": call_id, "skipped": "capture_failed"}
                if session is None:
                    recovered = archive.recover(row["audio_dir"]) if row["audio_dir"] else None
                    stats = {"call_id": call_id, "recovered": recovered is not None,
                             "duration_s": (recovered or {}).get("duration_s")}
                fields = {}
                if row["ended_at"] is None:
                    fields["ended_at"] = stores.now()
                if row["wf_state"] == "live":
                    fields["wf_state"] = "captured"
                if fields:
                    repo.update_call(conn, call_id, **fields)
                published = bus.publish(conn, Event(
                    type="CALL_ENDED", entity_id=call_id, dedupe_key=f"CALL_ENDED:{call_id}",
                    payload={"call_id": call_id, "audio_dir": row["audio_dir"],
                             "duration_s": stats.get("duration_s"), "died_early": stats.get("died_early", False)}))
                conn.commit()
            finally:
                conn.close()
            if session is not None or fields:
                self.hub.publish(hub_module.topic_for(call_id), {
                    "type": "ended", "call_id": call_id,
                    "stats": {k: stats.get(k) for k in ("duration_s", "turns", "died_early", "alerts")}})
            return {**stats, "call_id": call_id, "call_ended_published": published}

    def recover_orphans(self) -> list[str]:
        """Calls left 'live' by a crashed process: finalize audio and end them."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT node_id FROM calls WHERE source='capture' AND wf_state='live'").fetchall()
            finally:
                conn.close()
            orphans = [r["node_id"] for r in rows if r["node_id"] != self.active_call_id]
            for call_id in orphans:
                self.stop_call(call_id)
            return orphans

    def status(self) -> dict:
        # no lock: stop_call holds it while the transcriber drains, and the UI
        # polls status the whole time. Reading two references is atomic enough.
        session, call = self._session, self._call
        if session is None or not call:
            return {"active": False}
        return {"active": True, "call_id": call["call_id"], "title": call["title"],
                "deal_id": call["deal_id"], "lang_mode": call["lang_mode"],
                "started_at": call["started_at"], "elapsed_s": round(time.monotonic() - call["t0"], 1),
                **session.status()}


_manager: Optional[LiveManager] = None
_manager_lock = threading.Lock()


def get_manager() -> LiveManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LiveManager()
        return _manager
