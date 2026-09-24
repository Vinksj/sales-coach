"""Plugin: live coaching wired into `salescoach serve`, the web UI and the CLI.

  start_background  a supervisor thread polls the live manager once a second,
                    attaches a LiveCoach when a call goes live and finalizes
                    it when the call ends. Coaching is a passenger: if the
                    live stack cannot import, the supervisor logs and stops,
                    and capture carries on untouched.
  routes            GET  /coach/live/stream                  SSE for the overlay and the live page
                    GET  /coach/live/{call_id}/nudges         JSON
                    POST /coach/live/{call_id}/nudges/{id}/dismiss
                    GET  /coach/live/{call_id}                post-call nudge timeline
                    POST /coach/replay/{call_id}              replay a finished call (demo, tuning)
  CLI               salescoach coach-replay CALL_ID [--speed 20] [--no-slow] [--max-slow N]

The stream follows the hub topic coach:current, which carries only what the
screen needs: a status (idle | listening), the one nudge being shown, and a
clear when it is dismissed. It sends the current status on connect, so an
overlay that reconnects is right immediately.
"""
import asyncio
import json
import logging
import queue
import threading
import time
import uuid

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .. import identity

log = logging.getLogger("salescoach.plugins.live_coach")

NAV: list = []
router = APIRouter()
CURRENT_TOPIC = "coach:current"
KEEPALIVE_S = 15.0
STREAM_MAX_S = None            # tests bound the otherwise endless overlay stream
_engines: dict = {}            # call_id -> LiveCoach currently running in this process
_replay = {"thread": None, "call_id": None, "session": None}
_lock = threading.Lock()


def _global_hub():
    from ..live import hub as hub_module
    return hub_module.hub


# ---- background supervisor -----------------------------------------------------

class Supervisor:
    def __init__(self, db_path=None, status_fn=None, hub=None, factory=None, poll_s: float = 1.0):
        self.db_path = db_path
        self.status_fn = status_fn
        self.hub = hub or _global_hub()
        self.factory = factory
        self.poll_s = poll_s
        self.engine = None

    def _status(self) -> dict:
        try:
            return dict(self.status_fn() or {})
        except Exception:
            log.exception("live status failed")
            return {"active": False}

    def step(self) -> None:
        st = self._status()
        call_id = st.get("call_id") if st.get("active") else None
        if call_id and (self.engine is None or self.engine.call_id != call_id):
            if self.engine is not None:
                self._finish()
            self._attach(call_id, st.get("title"))
        elif not call_id and self.engine is not None:
            self._finish()

    def _attach(self, call_id, title) -> None:
        from ..coach.engine import LiveCoach, publish_status
        try:
            engine = (self.factory or LiveCoach)(call_id, self.db_path, self.hub)
            engine.attach(threaded=True)
        except Exception:
            log.exception("could not attach the live coach to %s", call_id)
            return
        self.engine = engine
        with _lock:
            _engines[call_id] = engine
        publish_status(self.hub, "listening", call_id, title)

    def _finish(self) -> None:
        from ..coach.engine import publish_status
        engine, self.engine = self.engine, None
        try:
            engine.end()
            engine.wait(15)
        except Exception:
            log.exception("live coach finalize failed")
        with _lock:
            _engines.pop(engine.call_id, None)
        publish_status(self.hub, "idle")

    def run(self, stop) -> None:
        from ..coach.engine import publish_status
        publish_status(self.hub, "idle")
        try:
            while not stop.wait(self.poll_s):
                self.step()
        finally:
            if self.engine is not None:
                self._finish()


def start_background(db_path, stop):
    if identity.cloud():
        log.info("live coach disabled: no live capture in a cloud install")
        return
    try:
        from ..live.manager import get_manager
    except Exception:
        log.exception("live coach disabled: the live manager does not import")
        return
    stop = stop or threading.Event()
    sup = Supervisor(db_path, status_fn=lambda: get_manager().status())
    threading.Thread(target=sup.run, args=(stop,), name="salescoach-live-coach", daemon=True).start()


# ---- routes --------------------------------------------------------------------

def _hub(request: Request):
    return getattr(request.app.state, "hub", None) or _global_hub()


def _sse(message: dict) -> str:
    return "data: " + json.dumps(message, default=str) + "\n\n"


def _db(request: Request):
    from ..store import stores
    return stores.sales(getattr(request.app.state, "db_path", None))


@router.get("/coach/live/stream")
async def coach_stream(request: Request):
    hub = _hub(request)
    q = hub.subscribe(CURRENT_TOPIC, maxsize=200)

    async def stream():
        try:
            yield "retry: 3000\n\n"
            latest = hub.latest(CURRENT_TOPIC)
            yield _sse(latest.get("status") or {"type": "status", "state": "idle"})
            nudge, clear = latest.get("nudge"), latest.get("clear")
            if nudge and time.time() < float(nudge.get("ts", 0)) + float(nudge.get("ttl_s", 12)) \
                    and not (clear and clear.get("id") == nudge.get("id")):
                yield _sse(nudge)
            started = last = time.monotonic()
            while True:
                if STREAM_MAX_S is not None and time.monotonic() - started > STREAM_MAX_S:
                    return
                try:
                    message = q.get_nowait()
                except queue.Empty:
                    if time.monotonic() - last >= KEEPALIVE_S:
                        yield ": keepalive\n\n"
                        last = time.monotonic()
                    await asyncio.sleep(0.1)
                    continue
                yield _sse(message)
                last = time.monotonic()
        finally:
            hub.unsubscribe(CURRENT_TOPIC, q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/coach/live/{call_id}/nudges")
def coach_nudges(request: Request, call_id: str, session: str | None = None):
    from ..coach import report
    conn = _db(request)
    try:
        chosen, rows = report.rows(conn, call_id, session)
        return JSONResponse({"call_id": call_id, "session": chosen, "sessions": report.sessions(conn, call_id),
                             "summary": report.summarise(rows), "nudges": rows})
    finally:
        conn.close()


@router.post("/coach/live/{call_id}/nudges/{nudge_id}/dismiss")
def coach_dismiss(request: Request, call_id: str, nudge_id: int):
    conn = _db(request)
    try:
        row = conn.execute("SELECT id FROM nudges WHERE id=? AND call_id=?", (nudge_id, call_id)).fetchone()
        if row is None:
            raise HTTPException(404, "no such nudge")
        conn.execute("UPDATE nudges SET dismissed=1 WHERE id=?", (nudge_id,))
        conn.commit()
    finally:
        conn.close()
    with _lock:
        engine = _engines.get(call_id)
    if engine is not None:
        engine.dismiss(nudge_id)
    hub = _hub(request)
    clear = {"type": "clear", "id": nudge_id, "call_id": call_id}
    hub.publish(f"coach:{call_id}", clear)
    hub.publish(CURRENT_TOPIC, clear)
    return {"ok": True, "id": nudge_id}


@router.get("/coach/live/{call_id}", response_class=HTMLResponse)
def coach_timeline(request: Request, call_id: str, session: str | None = None):
    from .. import repo
    from ..coach import report, settings
    from ..web.app import render
    conn = _db(request)
    try:
        call = repo.get_call(conn, call_id)
        if call is None:
            raise HTTPException(404, "no such call")
        chosen, rows = report.rows(conn, call_id, session)
        has_turns = conn.execute("SELECT 1 FROM turns WHERE call_id=? LIMIT 1", (call_id,)).fetchone() is not None
        cfg = settings.load()
        labels = {k: v.get("label", k) for k, v in cfg["triggers"].items()}
        with _lock:
            replaying = _replay["thread"] is not None and _replay["thread"].is_alive()
        return render(request, conn, "coach_live_timeline.html", call=call, session=chosen, rows=rows,
                      shown=[r for r in rows if r["shown"]], suppressed=[r for r in rows if not r["shown"]],
                      summary=report.summarise(rows), sessions=report.sessions(conn, call_id), labels=labels,
                      budget=cfg["budget"], has_turns=has_turns, replaying=replaying,
                      replay_call=_replay["call_id"] if replaying else None)
    finally:
        conn.close()


@router.post("/coach/replay/{call_id}")
def coach_replay(request: Request, call_id: str, speed: float = Form(20.0), slow: str = Form("")):
    from .. import repo
    from ..coach import replay as replay_mod
    from ..coach.engine import publish_status
    from ..web.app import _live_status, _redirect
    conn = _db(request)
    try:
        call = repo.get_call(conn, call_id)
        if call is None:
            raise HTTPException(404, "no such call")
        has_turns = conn.execute("SELECT 1 FROM turns WHERE call_id=? LIMIT 1", (call_id,)).fetchone() is not None
    finally:
        conn.close()
    back = f"/coach/live/{call_id}"
    if not has_turns:
        return _redirect(back, err="This call has no transcript to replay.")
    if _live_status(request.app).get("active"):
        return _redirect(back, err="A call is live. Replays wait until it ends, so the overlay stays on the real call.")
    with _lock:
        if _replay["thread"] is not None and _replay["thread"].is_alive():
            return _redirect(back, err=f"A replay of {_replay['call_id']} is already running.")
        session = f"replay-{uuid.uuid4().hex[:8]}"
        hub, db_path = _hub(request), getattr(request.app.state, "db_path", None)
        use_slow = slow.lower() in ("1", "on", "true", "yes")

        def register(engine):
            with _lock:
                _engines[call_id] = engine

        actor = identity.current_actor()                 # captured here: a new thread has no request context

        def run():
            publish_status(hub, "listening", call_id, call["title"], mode="replay")
            try:
                with identity.activate(actor):
                    replay_mod.replay(call_id, db_path=db_path, speed=max(0.0, speed), slow=use_slow, publish_hub=hub,
                                      publish_current=True, session=session, on_engine=register)
            except Exception:
                log.exception("replay of %s failed", call_id)
            finally:
                with _lock:
                    _engines.pop(call_id, None)
                publish_status(hub, "idle")

        thread = threading.Thread(target=run, name=f"coach-replay-{call_id}", daemon=True)
        _replay.update(thread=thread, call_id=call_id, session=session)
        thread.start()
    return _redirect(f"{back}?session={session}",
                     msg=f"Replay started at {speed:g}x{' with' if use_slow else ' without'} the slow pass. "
                         "Reload to watch nudges arrive.")


# ---- CLI -----------------------------------------------------------------------

def cmd_coach_replay(args):
    from .. import repo
    from ..coach import replay as replay_mod
    from ..coach import report
    from ..store import stores
    overrides = {"slow": {"max_passes": args.max_slow}} if args.max_slow else None
    summary = replay_mod.replay(args.call_id, speed=args.speed, slow=not args.no_slow, overrides=overrides,
                                duration_s=args.duration_min * 60 if args.duration_min else None)
    conn = stores.sales()
    try:
        call = dict(repo.get_call(conn, args.call_id))
        _, rows = report.rows(conn, args.call_id, summary["session"])
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"summary": summary, "nudges": rows}, indent=2, default=str))
        return
    print(report.timeline_text(call, summary["session"], rows, verbose=args.all))
    st = summary["stats"]
    print(f"\n{summary['segments']} segments over {summary['t_end'] / 60:.1f} min "
          f"({'timed' if summary['timed'] else 'estimated timing'}); fast path max {st['fast_ms_max']} ms; "
          f"slow passes {st['slow_passes']} (errors {st['slow_errors']}, late {st['slow_late']}, "
          f"avg {st['slow_latency_avg_s']} s)")


def register_cli(subparsers):
    p = subparsers.add_parser("coach-replay", help="replay a finished call through the live coach")
    p.add_argument("call_id")
    p.add_argument("--speed", type=float, default=20.0, help="replay speed factor (0 = as fast as possible)")
    p.add_argument("--no-slow", action="store_true", help="fast rules only, no Claude pass")
    p.add_argument("--max-slow", type=int, default=None, metavar="N",
                   help="at most N Claude slow passes (caps quota spend on a long replay)")
    p.add_argument("--duration-min", type=float, default=None,
                   help="stretch estimated timing to this call length (text-only calls)")
    p.add_argument("--all", action="store_true", help="list every suppressed candidate")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_coach_replay)
