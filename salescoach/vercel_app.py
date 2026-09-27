"""The ASGI entrypoint of a Vercel deployment (pyproject.toml: [tool.vercel] entrypoint = "salescoach.vercel_app:app").

What `salescoach serve --role web` is on a container, for a platform with no long-lived process
(salescoach/serverless.py, docs/deploy-vercel.md):

  * At cold start it runs the checks `serve` runs before a cloud web process starts (serverless.startup_problems:
    endpoint overrides, the mode, Postgres, the session secret, the Google client, the token keys, the serving
    role, a pooled URL, the cron secret). With a problem it does NOT crash (a function that raises at import is
    retried by the platform, a crash loop that says nothing): every request gets a 500 page naming the problems
    (none of them contains a secret), /health a 503 with the same list, and the checks run again at most every
    RECHECK_S, so fixing the environment or the database heals the instance without a redeploy.
  * The web app is the `web` role with start_worker=False: no worker, no scheduler, no heartbeat, no Jarvis sync,
    no embed loop, no live hub. Nothing runs after a response.
  * The worker and the scheduler are two cron endpoints (salescoach/cron.py) answered HERE, in front of the app's
    own middleware (the same-origin guard, the sign-in gate, the actor and first-run gates are for browsers):
    GET /cron/drain and GET /cron/tick, only with `Authorization: Bearer $CRON_SECRET` (constant-time compare;
    refused while CRON_SECRET is unset). A container build never mounts them.
"""
import asyncio
import html
import json
import logging
import os
import time

os.environ.setdefault("SALESCOACH_PLATFORM", "vercel")      # this module IS the serverless entrypoint

from . import serverless  # noqa: E402

log = logging.getLogger("salescoach.vercel")
CRON_PATHS = {"/cron/drain": "drain", "/cron/tick": "tick"}
RECHECK_S = 30.0


async def _send(send, status: int, body: bytes, content_type: str, headers=()) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type.encode()), (b"cache-control", b"no-store"),
                            *[(k.encode(), v.encode()) for k, v in headers]]})
    await send({"type": "http.response.body", "body": body})


async def _json(send, status: int, payload: dict) -> None:
    await _send(send, status, json.dumps(payload).encode(), "application/json")


def _problem_page(problems: list) -> bytes:
    items = "".join(f"<li>{html.escape(p)}</li>" for p in problems)
    return (f'<!doctype html><meta charset="utf-8"><title>Not configured</title><main style="font:16px/1.5 '
            f'system-ui;max-width:46rem;margin:3rem auto;padding:0 1rem"><p>500</p><h1>This deployment is not '
            f'ready to serve</h1><p>It refused to start in cloud mode:</p><ul>{items}</ul><p>See '
            f'docs/deploy-vercel.md. Fix the environment (or the database) and reload: the checks run again '
            f'within {int(RECHECK_S)} seconds, no redeploy needed.</p></main>').encode()


class ServerlessApp:
    """The function's ASGI app: the cron door, then either the web app or the problem page."""

    def __init__(self, build=None, clock=time.monotonic):
        self._build = build or self._default_build
        self._clock = clock
        self.app = None
        self.problems: list = []
        self.checked_at = None
        self._lock = asyncio.Lock()
        self._attempt()

    @staticmethod
    def _default_build():
        removed = serverless.scrub_owner_env()
        if removed:
            log.info("removed the database owner's variables from this function's environment: %s", ", ".join(removed))
        try:
            problems = serverless.startup_problems()
        except Exception as exc:                    # a check that cannot run is itself the problem, said plainly
            problems = [f"the startup checks could not run: {type(exc).__name__}: {str(exc)[:200]}"]
        if problems:
            return None, problems
        from .web.app import create_app
        return create_app(start_worker=False, role="web", trusted_origins=set()), []

    def _attempt(self) -> None:
        self.checked_at = self._clock()
        try:
            self.app, self.problems = self._build()
        except Exception as exc:
            self.app, self.problems = None, [f"the app could not be built: {type(exc).__name__}: {str(exc)[:200]}"]
        for problem in self.problems:
            log.error("refusing to serve: %s", problem)

    async def _ready(self) -> bool:
        if self.app is not None:
            return True
        async with self._lock:
            if self.app is None and self._clock() - self.checked_at >= RECHECK_S:
                from starlette.concurrency import run_in_threadpool
                await run_in_threadpool(self._attempt)
        return self.app is not None

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":                      # nothing starts and nothing stops: acknowledge, never forward
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if kind != "http":
            if await self._ready():
                await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        if path in CRON_PATHS:
            await self._cron(scope, send, CRON_PATHS[path])
            return
        if await self._ready():
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if path == "/health" or "text/html" not in headers.get("accept", ""):
            await _json(send, 503 if path == "/health" else 500, {"status": "error", "problems": self.problems})
        else:
            await _send(send, 500, _problem_page(self.problems), "text/html; charset=utf-8")

    async def _cron(self, scope, send, job: str) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if scope["method"].upper() != "GET":
            await _send(send, 405, b"GET only", "text/plain", headers=[("allow", "GET")])
            return
        if not serverless.cron_authorized(headers.get("authorization")):
            if serverless.cron_secret() is None:
                log.error("cron %s refused: CRON_SECRET is not set (or shorter than %d characters)", job,
                          serverless.CRON_SECRET_MIN)
            await _json(send, 401, {"error": "unauthorized"})
            return
        if not await self._ready():
            await _json(send, 500, {"status": "error", "problems": self.problems})
            return
        from starlette.concurrency import run_in_threadpool
        from . import cron
        db_path = getattr(self.app.state, "db_path", None)
        try:
            result = await run_in_threadpool(cron.drain if job == "drain" else cron.tick, db_path)
        except Exception as exc:
            log.exception("cron %s failed", job)
            await _json(send, 500, {"status": "error", "job": job, "error": type(exc).__name__})
            return
        await _json(send, 200, {"status": "ok", "job": job, **result})


app = ServerlessApp()
