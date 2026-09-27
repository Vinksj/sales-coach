"""The Vercel entrypoint (salescoach/vercel_app.py) and what the web app does differently on a serverless platform.

  * the cron door: GET /cron/drain and /cron/tick answer only `Bearer $CRON_SECRET` (constant time), refuse
    everything while CRON_SECRET is unset, take GET only, and are answered in front of the browser gates (a password
    gate and a public-URL host check refuse "/" while the cron call passes); a container app has no such route;
  * a cold start with a problem serves a 500 page naming it (and /health a 503), never crashes, and heals by itself
    once the problem is fixed (the checks run again after RECHECK_S);
  * the startup checks are `serve`'s own: cloud mode, Postgres, the cloud settings, a pooled URL, the cron secret,
    the serving role (Postgres: the owner role is refused);
  * nothing starts in the background, whatever create_app is asked;
  * the UI: the worker pill follows the last cron drain; audio upload and the live coach are off with a reason;
    the transcript upload cap stays under the platform's body limit and is refused with a clear message.
"""
import threading

import pytest
from fastapi.testclient import TestClient

import conftest
from salescoach import cron, hosted, identity, serverless
from salescoach.web.app import create_app

SECRET = "cron-secret-for-the-tests-0123456789"


@pytest.fixture
def on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv(serverless.CRON_SECRET_ENV, SECRET)
    return monkeypatch


def _serverless(build):
    from salescoach import vercel_app
    return vercel_app.ServerlessApp(build=build)


def _client(asgi, **kw):
    return TestClient(asgi, follow_redirects=False, raise_server_exceptions=False, **kw)


def _web():
    return create_app(start_worker=False, role="web", trusted_origins=set())


def test_the_cron_door_takes_only_the_bearer_secret(db, on_vercel):
    from salescoach import plugins
    from salescoach.automation.scheduler import Duty
    ran = []                                        # the real duties would reach Gmail, calendars, `claude -p`
    on_vercel.setattr(plugins, "cron_duties", lambda: [Duty("probe", lambda conn: ran.append(1), lambda: 600.0)])
    client = _client(_serverless(lambda: (_web(), [])))
    assert client.get("/cron/drain").status_code == 401
    assert client.get("/cron/drain", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/cron/drain", headers={"authorization": SECRET}).status_code == 401
    assert client.post("/cron/drain", headers={"authorization": f"Bearer {SECRET}"}).status_code == 405
    ok = client.get("/cron/drain", headers={"authorization": f"Bearer {SECRET}"})
    assert ok.status_code == 200 and ok.json()["job"] == "drain" and ok.json()["stopped"] == "empty"
    tick = client.get("/cron/tick", headers={"authorization": f"Bearer {SECRET}"})
    assert tick.status_code == 200 and tick.json()["job"] == "tick" and tick.json()["ran"] == ["probe"] and ran == [1]
    on_vercel.delenv(serverless.CRON_SECRET_ENV)
    assert client.get("/cron/drain", headers={"authorization": "Bearer "}).status_code == 401
    assert client.get("/cron/drain", headers={"authorization": f"Bearer {SECRET}"}).status_code == 401


def test_cron_calls_pass_in_front_of_the_browser_gates(db, on_vercel):
    on_vercel.setenv("SALESCOACH_PASSWORD", "a password for the gate")
    on_vercel.setenv("SALESCOACH_PUBLIC_URL", "https://coach.example.test")
    client = _client(_serverless(lambda: (_web(), [])), base_url="https://abc123.vercel.app")
    assert client.get("/", headers={"accept": "text/html"}).status_code == 403          # unknown host
    ok = client.get("/cron/drain", headers={"authorization": f"Bearer {SECRET}"})
    assert ok.status_code == 200, ok.text                                               # the deployment's own URL


def test_a_container_app_has_no_cron_route(db):
    client = _client(create_app(start_worker=False))
    r = client.get("/cron/drain", headers={"authorization": f"Bearer {SECRET}"})
    assert r.status_code == 404
    assert cron.DRAIN_KEY not in {row[0] for row in db.execute("SELECT key FROM state").fetchall()}


def test_a_cold_start_with_a_problem_says_so_and_heals(db, on_vercel):
    from salescoach import vercel_app
    now = {"t": 0.0}
    attempts = []

    def build():
        attempts.append(now["t"])
        if len(attempts) == 1:
            return None, ["SALESCOACH_SESSION_SECRET is not set (a long random string; it signs the session cookie)"]
        return _web(), []
    asgi = vercel_app.ServerlessApp(build=build, clock=lambda: now["t"])
    client = _client(asgi)
    page = client.get("/", headers={"accept": "text/html"})
    assert page.status_code == 500 and "SALESCOACH_SESSION_SECRET is not set" in page.text
    health = client.get("/health")
    assert health.status_code == 503 and health.json()["problems"][0].startswith("SALESCOACH_SESSION_SECRET")
    assert client.get("/cron/drain", headers={"authorization": f"Bearer {SECRET}"}).status_code == 500
    assert len(attempts) == 1                                                  # not re-checked on every request
    now["t"] += vercel_app.RECHECK_S
    assert client.get("/health").status_code == 200 and len(attempts) == 2    # fixed: serving, no redeploy


def test_a_build_that_raises_is_a_problem_not_a_crash(db, on_vercel):
    def build():
        raise RuntimeError("the schema is at version 9 and this build expects 10")
    client = _client(_serverless(build))
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 500 and "expects 10" in r.text


def test_the_startup_checks_are_the_serve_checks(db, on_vercel):
    for name in ("SALESCOACH_MODE", "DATABASE_URL", "SALESCOACH_DATABASE_URL"):
        on_vercel.delenv(name, raising=False)
    [problem] = serverless.startup_problems()
    assert "must be cloud" in problem
    on_vercel.setenv(identity.MODE_ENV, "cloud")
    assert "needs Postgres" in serverless.startup_problems()[0]
    on_vercel.setenv(serverless.DATABASE_URL_ENV, "postgresql://salescoach_app:pw@ep-x-pooler.eu.aws.neon.tech/db")
    on_vercel.delenv(serverless.CRON_SECRET_ENV)
    problems = " | ".join(serverless.startup_problems())
    for expected in ("SALESCOACH_SESSION_SECRET", "GOOGLE_CLIENT_ID", "SALESCOACH_TOKEN_KEYS", "CRON_SECRET",
                     "connection pooler"):
        assert expected in problems, expected
    assert "pw@" not in problems                                               # no secret in any message


@pytest.mark.postgres_only
def test_on_postgres_the_owner_role_is_refused_and_the_app_role_serves(db, on_vercel):
    from test_google_auth import K1
    on_vercel.setenv(identity.MODE_ENV, "cloud")
    on_vercel.setenv(hosted.SESSION_SECRET_ENV, "a long random string for the tests")
    on_vercel.setenv("GOOGLE_CLIENT_ID", "x.apps.googleusercontent.com")
    on_vercel.setenv("GOOGLE_CLIENT_SECRET", "shh")
    on_vercel.setenv("GOOGLE_ALLOWED_DOMAINS", "tessel.test")
    on_vercel.setenv("SALESCOACH_TOKEN_KEYS", K1)
    on_vercel.setenv(serverless.DATABASE_URL_ENV, conftest.PG_URL)
    [problem] = serverless.startup_problems()
    assert "row-level security would not apply" in problem
    on_vercel.setenv(serverless.DATABASE_URL_ENV, conftest.APP_URL)
    assert serverless.startup_problems() == []


def test_nothing_starts_in_the_background_on_vercel(db, on_vercel):
    before = {t.name for t in threading.enumerate()}
    app = create_app(start_worker=True, role="all", heartbeats=True)          # even when asked
    assert app.state.start_worker is False and app.state.platform == "vercel" and app.state.hub is None
    with TestClient(app) as client:                                           # the lifespan runs
        assert client.get("/health").json()["platform"] == "vercel"
        started = {t.name for t in threading.enumerate()} - before
    assert not [n for n in started if n.startswith(("salescoach-", "coach-"))], started


def test_the_worker_pill_follows_the_last_cron_drain(db, on_vercel):
    client = _client(_web())
    assert "worker off" in client.get("/").text
    assert client.get("/health").json()["worker"] == "cron (no recent drain)"
    cron.drain(budget_s=5)
    assert "worker off" not in client.get("/").text
    assert client.get("/health").json()["worker"] == "cron"


def test_audio_upload_and_the_live_coach_are_off_with_a_reason(db, on_vercel):
    client = _client(_web(), headers={"origin": "http://testserver"})
    page = client.get("/import")
    assert page.status_code == 200 and 'action="/import/audio"' not in page.text
    assert "Recording upload is not available on this deployment" in page.text
    r = client.post("/import/audio", files={"file": ("call.wav", b"RIFF....", "audio/wav")})
    assert r.status_code == 303 and "not+available" in r.headers["location"]
    assert client.get("/coach/live/stream").status_code == 404
    assert client.post("/coach/replay/c-1", data={"speed": "20"}).status_code == 404


def test_uploads_stay_under_the_platform_body_limit(db, on_vercel):
    client = _client(_web(), headers={"origin": "http://testserver"})
    page = client.get("/import")
    assert 'data-max-bytes="' in page.text and "Up to 4.2 MB" in page.text
    big = b"Me: hello\n" * 460_000                                               # 4.6 MB: over the platform's limit
    r = client.post("/import/file", files={"file": ("t.txt", big, "text/plain")})
    assert r.status_code == 413 and "limit for this upload is 4.2 MB" in r.text
    on_vercel.delenv("VERCEL")
    plain = _client(create_app(start_worker=False), headers={"origin": "http://testserver"})
    assert plain.post("/import/file", files={"file": ("t.txt", big, "text/plain")}).status_code != 413
