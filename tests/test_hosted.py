"""Hosted mode (salescoach/hosted.py, web/auth.py, the proxy-aware guard, /health, `serve` on 0.0.0.0).

Pinned here: without SALESCOACH_PASSWORD nothing changes; with it every page needs a session and
JSON / streams get a 401; the cookie is signed, expires, and a tampered one is nothing; five wrong
passwords lock an address out; `next` never leaves the app; the webhook still answers to its secret
with no session; /health is open and names no secret; in proxy mode Origin and Host are checked
against the public URL and forwarding headers are accepted (still refused on localhost); `serve`
refuses a non-loopback bind with no password; a PBKDF2 hash is accepted.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from salescoach import cli, hosted, sources
from salescoach.web.app import create_app
from test_sources_support import NAMED, generic_bytes

TWO = [{"speaker": "Maya Iyer", "text": "hello"}, {"speaker": "Ravi", "text": "hi there"}]

PASSWORD = "correct horse battery staple"
PUBLIC = "https://coach.example.test"
LOCAL_ORIGIN = {"origin": "http://127.0.0.1:8140"}
PUBLIC_ORIGIN = {"origin": PUBLIC, "host": "coach.example.test", "x-forwarded-for": "203.0.113.9",
                 "x-forwarded-proto": "https"}
HOOK = "/import/webhook"


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setenv("SALESCOACH_PASSWORD", PASSWORD)
    monkeypatch.delenv("SALESCOACH_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("SALESCOACH_SESSION_SECRET", raising=False)
    return PASSWORD


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setenv("SALESCOACH_PUBLIC_URL", PUBLIC)
    monkeypatch.delenv("SALESCOACH_TRUST_PROXY", raising=False)
    return PUBLIC


@pytest.fixture
def local(monkeypatch):
    for name in ("SALESCOACH_PASSWORD", "SALESCOACH_PASSWORD_HASH", "SALESCOACH_PUBLIC_URL", "SALESCOACH_TRUST_PROXY",
                 "SALESCOACH_SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client(db):
    app = create_app(start_worker=False, live_factory=None, hub=None)
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


@pytest.fixture
def pclient(db, proxy):
    """A browser at the public URL: Host is the public host, cookies are sent over https."""
    app = create_app(start_worker=False, live_factory=None, hub=None, trusted_origins=set())   # as a real server
    return TestClient(app, base_url=PUBLIC, follow_redirects=False, raise_server_exceptions=False)


def login(client, password=PASSWORD, nxt=None, **headers):
    data = {"password": password}
    if nxt is not None:
        data["next"] = nxt
    return client.post("/login", data=data, headers={**LOCAL_ORIGIN, **headers})


def _post_hook(client, secret, payload, **headers):
    return client.post(HOOK, headers={"x-salescoach-secret": secret, **headers}, content=generic_bytes(**payload))


# ---- no password: the localhost tool, unchanged -------------------------------------------------------------

def test_without_a_password_nothing_asks_for_one(client, local):
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 303 and client.get("/login").headers["location"] == "/"
    assert client.get("/health").status_code == 200
    assert client.post("/deals", data={"name": "X"}, headers=LOCAL_ORIGIN).status_code == 303


# ---- the login flow --------------------------------------------------------------------------------------------

def test_pages_redirect_to_login_and_json_gets_401(client, password):
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.get("/loops?owner=me", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Floops%3Fowner%3Dme"
    r = client.get("/loops", headers={"accept": "application/json"})
    assert r.status_code == 401 and r.json() == {"error": "login required"}
    r = client.get("/live/x/events", headers={"accept": "text/event-stream"})
    assert r.status_code == 401
    r = client.get("/live/x/events", headers={"accept": "text/html"})            # a stream is never a page
    assert r.status_code == 401
    assert client.post("/deals", data={"name": "X"}, headers=LOCAL_ORIGIN).status_code == 401
    assert client.get("/static/app.css").status_code == 200                       # the login page needs its stylesheet
    assert client.get("/setup", headers={"accept": "text/html"}).status_code == 303   # setup is not a way around it


def test_login_page_renders_and_never_echoes_the_password(client, password):
    r = client.get("/login")
    assert r.status_code == 200 and 'name="password"' in r.text and "Log out" not in r.text
    r = login(client, "wrong password")
    assert r.status_code == 401 and "not right" in r.text and "wrong password" not in r.text
    assert PASSWORD not in r.text and "set-cookie" not in r.headers


def test_right_password_logs_in_rotates_the_cookie_and_logout_ends_it(client, password):
    r = login(client, nxt="/loops")
    assert r.status_code == 303 and r.headers["location"] == "/loops"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{hosted.COOKIE}=") and "HttpOnly" in cookie and "SameSite=lax" in cookie.lower().replace(
        "samesite=lax", "SameSite=lax") and "Secure" not in cookie
    assert f"Max-Age={hosted.SESSION_S}" in cookie
    first = client.cookies.get(hosted.COOKIE)
    assert client.get("/").status_code == 200
    assert "Log out" in client.get("/").text
    assert client.get("/login").status_code == 303                                 # already in: straight through
    time.sleep(1.1)
    login(client)
    assert client.cookies.get(hosted.COOKIE) != first                              # rotated: a new issued_at
    r = client.post("/logout", headers=LOCAL_ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_cookie_is_signed_tampering_and_expiry_are_nothing(password):
    value = hosted.issue_session(now=1_000_000)
    user, issued, expiry, sig = value.split("|")
    assert (user, issued, expiry) == ("seller", "1000000", str(1_000_000 + hosted.SESSION_S))
    assert hosted.verify_session(value, now=1_000_001)["user"] == "seller"
    assert hosted.verify_session(value, now=1_000_000 + hosted.SESSION_S) is None          # expired
    assert hosted.verify_session(f"seller|{issued}|{int(expiry) + 5}|{sig}", now=1_000_001) is None   # stretched
    assert hosted.verify_session(f"admin|{issued}|{expiry}|{sig}", now=1_000_001) is None            # renamed
    assert hosted.verify_session(value[:-1] + ("0" if value[-1] != "0" else "1"), now=1_000_001) is None
    assert hosted.verify_session("", now=1) is None and hosted.verify_session(None) is None
    assert hosted.verify_session("a|b|c", now=1) is None and hosted.verify_session("a|b|c|d|e", now=1) is None


def test_a_cookie_for_another_secret_or_password_is_refused(client, password, monkeypatch):
    login(client)
    assert client.get("/").status_code == 200
    monkeypatch.setenv("SALESCOACH_PASSWORD", "a new password")            # rotation logs everyone out
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303
    monkeypatch.setenv("SALESCOACH_PASSWORD", PASSWORD)
    assert client.get("/").status_code == 200
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "an explicit secret")
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_secure_cookie_only_over_https(client, password, monkeypatch):
    monkeypatch.setenv("SALESCOACH_PUBLIC_URL", "http://127.0.0.1:8140")
    assert "Secure" not in login(client).headers["set-cookie"]
    monkeypatch.setenv("SALESCOACH_PUBLIC_URL", PUBLIC)
    r = login(client, **PUBLIC_ORIGIN)
    assert r.status_code == 303 and "Secure" in r.headers["set-cookie"]


def test_five_failures_lock_the_address_out_for_fifteen_minutes(client, password):
    for _ in range(5):
        assert login(client, "nope").status_code == 401
    r = login(client, "nope")
    assert r.status_code == 429 and "Too many attempts" in r.text and "15 minutes" in r.text
    assert login(client).status_code == 429                                        # even the right one waits
    limiter = hosted.LoginLimiter()
    for i in range(5):
        limiter.failed("1.2.3.4", now=1000 + i)
    assert limiter.retry_after("1.2.3.4", now=1010) > 0
    assert limiter.retry_after("1.2.3.4", now=1000 + hosted.LOGIN_WINDOW_S + 1) == 0    # the window passed
    assert limiter.retry_after("5.6.7.8", now=1010) == 0                                  # another address
    limiter.failed("1.2.3.4", now=2000)
    limiter.succeeded("1.2.3.4")
    assert limiter.retry_after("1.2.3.4", now=2001) == 0
    # bounded: a flood of addresses is pruned, live lockouts are kept
    small = hosted.LoginLimiter()
    small.MAX_KEYS = 50
    for i in range(5):
        small.failed("locked", now=5000 + i)
    for i in range(200):
        small.failed(f"10.0.0.{i}", now=3000 if i < 100 else 5010)           # half of them long outside the window
    assert len(small._failures) <= 51 and small.retry_after("locked", now=5011) > 0


def test_rate_limit_keys_on_the_address_the_proxy_appended(pclient, db, password):
    spoof = {**PUBLIC_ORIGIN, "x-forwarded-for": "198.51.100.7, 203.0.113.9"}    # the caller prepends a fake hop
    for _ in range(5):
        assert login(pclient, "nope", **spoof).status_code == 401
    other_spoof = {**PUBLIC_ORIGIN, "x-forwarded-for": "198.51.100.8, 203.0.113.9"}
    assert login(pclient, "nope", **other_spoof).status_code == 429             # same real address: still locked
    assert login(pclient, "nope", **{**PUBLIC_ORIGIN, "x-forwarded-for": "203.0.113.10"}).status_code == 401


def test_next_never_leaves_the_app(client, password):
    for bad in ("https://evil.test/", "//evil.test/x", "/\\evil.test", "javascript:alert(1)", "", "/login", "/logout"):
        r = login(client, nxt=bad)
        assert r.status_code == 303 and r.headers["location"] == "/", bad
        client.cookies.clear()
    r = login(client, nxt="/calls/abc?msg=old#email")
    assert r.headers["location"] == "/calls/abc#email"                            # a stale flash is dropped
    client.cookies.clear()
    r = client.get("/login?next=https://evil.test/")
    assert 'value="/"' in r.text and "evil" not in r.text


# ---- the webhook and /health are outside the gate ---------------------------------------------------------------

def test_webhook_answers_to_its_secret_without_a_session(client, db, password):
    secret = sources.new_webhook_secret()
    assert _post_hook(client, secret, {"turns": TWO}).status_code == 201
    assert _post_hook(client, secret[:-1] + "x", {"turns": []}).status_code == 403
    assert client.post(HOOK, content=b"{}").status_code == 403                   # no secret: refused, not asked to log in
    assert client.get(HOOK, headers={"accept": "text/html"}).status_code == 303   # GET is a page like any other


def test_health_is_open_cheap_and_names_no_secret(client, db, password, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "hook-secret-DISTINCTIVE")
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "version", "db", "worker", "configured", "role", "processes"}
    assert body["status"] == "ok" and body["db"] == "ok" and body["worker"] == "off" and body["configured"] is True
    assert body["role"] == "all" and body["processes"] == {"workers": [], "schedulers": [], "leader": None}
    assert body["version"] and body["version"] != "unknown"
    assert PASSWORD not in r.text and "DISTINCTIVE" not in r.text
    assert "set-cookie" not in r.headers


def test_health_before_the_profile_exists(db, seller_settings, local):
    from conftest import write_seller
    (seller_settings / "seller.yaml").unlink()
    client = TestClient(create_app(start_worker=False, live_factory=None, hub=None), follow_redirects=False)
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["configured"] is False             # not bounced to /setup
    write_seller(seller_settings)


def test_health_reports_a_broken_store(db, local, monkeypatch, tmp_path):
    app = create_app(start_worker=False, live_factory=None, hub=None, db_path=tmp_path / "nowhere" / "x" / "sales.db")
    from salescoach.store import stores

    def broken(path=None):
        raise OSError("disk gone")

    monkeypatch.setattr(stores, "sales", broken)
    r = TestClient(app, raise_server_exceptions=False).get("/health")
    assert r.status_code == 503 and r.json()["status"] == "error" and r.json()["db"].startswith("error")


# ---- proxy mode: the guard checks the public URL -------------------------------------------------------------

def test_proxy_mode_accepts_the_public_origin_and_forwarding_headers(pclient, db, password):
    r = pclient.get("/", headers={"accept": "text/html", "x-forwarded-for": "203.0.113.9"})
    assert r.status_code == 303 and r.headers["location"] == "/login"             # forwarded, and asked to log in
    assert login(pclient, **PUBLIC_ORIGIN).status_code == 303
    page = {"x-forwarded-for": "203.0.113.9", "x-forwarded-proto": "https", "forwarded": "for=203.0.113.9"}
    assert pclient.get("/", headers=page).status_code == 200
    assert pclient.get("/", headers={**page, "host": "coach.example.test:443"}).status_code == 200
    assert pclient.get("/", headers={**page, "host": "COACH.example.test"}).status_code == 200
    assert pclient.get("/health", headers={"host": "127.0.0.1:8140"}).status_code == 200    # the container's own check
    assert pclient.get("/health", headers={"host": "localhost:8140"}).status_code == 200
    r = pclient.post("/deals", data={"name": "X"}, headers=PUBLIC_ORIGIN)
    assert r.status_code == 303
    r = pclient.post("/deals", data={"name": "X"}, headers={**PUBLIC_ORIGIN, "origin": "HTTPS://Coach.Example.test"})
    assert r.status_code == 303
    r = pclient.post("/deals", data={"name": "X"}, headers={**page, "referer": PUBLIC + "/deals"})   # Referer will do
    assert r.status_code == 303


def test_proxy_mode_refuses_other_hosts_and_origins(pclient, db, password):
    login(pclient, **PUBLIC_ORIGIN)
    fwd = {"x-forwarded-for": "203.0.113.9", "x-forwarded-proto": "https"}
    for host in ("evil.example.test", "coach.example.test:8443", "coach.example.test.evil.test", "coach.example.test:x"):
        r = pclient.get("/", headers={**fwd, "host": host})
        assert r.status_code == 403 and "unknown host" in r.text, host
    for origin in ("http://coach.example.test", "https://evil.example.test", "https://coach.example.test:8443",
                   "http://127.0.0.1:8140", "null", ""):
        r = pclient.post("/deals", data={"name": "X"}, headers={**PUBLIC_ORIGIN, "origin": origin})
        assert r.status_code == 403, origin
    # a loopback Host is served (the health check) but its origin cannot press a button
    r = pclient.post("/deals", data={"name": "X"}, headers={"host": "127.0.0.1:8140", "origin": "http://127.0.0.1:8140"})
    assert r.status_code == 403


def test_localhost_mode_still_refuses_forwarded_requests(client, db, local):
    assert client.get("/", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 403
    assert client.get("/", headers={"host": "coach.example.test"}).status_code == 403
    assert client.get("/").status_code == 200
    forged = {**LOCAL_ORIGIN, "x-forwarded-for": "203.0.113.9", "host": "127.0.0.1:8140"}
    assert client.post("/deals", data={"name": "X"}, headers=forged).status_code == 403


def test_proxy_mode_webhook_is_remote_so_allow_remote_is_required(pclient, db, password):
    secret = sources.new_webhook_secret()
    payload = {"turns": TWO}
    fwd = {"x-forwarded-for": "203.0.113.9"}
    r = _post_hook(pclient, secret, payload, **fwd)
    assert r.status_code == 403 and "tunnel" in r.json()["error"]
    assert _post_hook(pclient, secret, payload).status_code == 403               # even with no forwarding header
    assert _post_hook(pclient, secret, payload, host="127.0.0.1:8140").status_code == 403    # or a loopback Host
    sources.save("webhook", True, options={"allow_remote": True})
    assert _post_hook(pclient, secret, payload, **fwd).status_code == 201
    assert _post_hook(pclient, secret[:-1] + "x", payload, **fwd).status_code == 403
    assert pclient.post(HOOK, content=b"{}", headers=fwd).status_code == 403     # no secret, no session: refused


def test_public_url_parsing_and_host_matching(monkeypatch):
    monkeypatch.setenv("SALESCOACH_PUBLIC_URL", "https://Coach.Example.test/")
    assert hosted.public_origin() == "https://coach.example.test" and hosted.public_host() == ("coach.example.test", 443)
    assert hosted.host_matches_public("coach.example.test") and hosted.host_matches_public("coach.example.test:443")
    assert not hosted.host_matches_public("coach.example.test:80") and not hosted.host_matches_public("other.test")
    assert not hosted.host_matches_public("coach.example.test:abc") and not hosted.host_matches_public("")
    assert hosted.cookie_secure() and hosted.trusted_proxies() == "*"
    monkeypatch.setenv("SALESCOACH_TRUST_PROXY", "10.0.0.0/8")
    assert hosted.trusted_proxies() == "10.0.0.0/8"
    monkeypatch.setenv("SALESCOACH_PUBLIC_URL", "http://127.0.0.1:8189")
    assert hosted.public_host() == ("127.0.0.1", 8189) and not hosted.cookie_secure()
    assert hosted.host_matches_public("127.0.0.1:8189") and not hosted.host_matches_public("127.0.0.1")
    for bad in ("coach.example.test", "ftp://x", "", "   "):
        monkeypatch.setenv("SALESCOACH_PUBLIC_URL", bad)
        assert hosted.public_url() is None and not hosted.proxy_mode(), bad
    monkeypatch.delenv("SALESCOACH_TRUST_PROXY")
    assert hosted.trusted_proxies() is None


# ---- serve: no network bind without a password ------------------------------------------------------------------

def test_serve_refuses_a_non_loopback_bind_without_a_password(local, monkeypatch, capsys):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("salescoach.web.app.create_app", lambda *a, **k: object())
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert "no login" in capsys.readouterr().err and calls == []
    assert cli.main(["serve", "--host", "192.168.1.5", "--port", "9000"]) == 2 and calls == []
    cli.main(["serve"])                                                            # loopback: as before
    assert calls[-1]["host"] == "127.0.0.1" and "proxy_headers" not in calls[-1]
    cli.main(["serve", "--host", "::1"])
    assert calls[-1]["host"] == "::1"
    cli.main(["serve", "--host", "0.0.0.0", "--allow-unauthenticated"])
    assert calls[-1]["host"] == "0.0.0.0" and "proxy_headers" not in calls[-1]
    monkeypatch.setenv("SALESCOACH_PASSWORD", "x")
    cli.main(["serve", "--host", "0.0.0.0", "--port", "8140"])
    assert calls[-1]["host"] == "0.0.0.0" and "proxy_headers" not in calls[-1]     # a password, no proxy yet
    monkeypatch.setenv("SALESCOACH_PUBLIC_URL", PUBLIC)
    cli.main(["serve", "--host", "0.0.0.0"])
    assert calls[-1]["proxy_headers"] is True and calls[-1]["forwarded_allow_ips"] == "*"
    monkeypatch.setenv("SALESCOACH_TRUST_PROXY", "172.16.0.0/12")
    cli.main(["serve", "--host", "0.0.0.0"])
    assert calls[-1]["forwarded_allow_ips"] == "172.16.0.0/12"


def test_serve_in_cloud_mode_binds_the_network_without_a_password(local, monkeypatch, capsys):
    """Regression (e2e run 2026-09-25): cloud mode's login is Google sign-in, never a password, and the image's
    CMD binds 0.0.0.0, so `serve --role web` in a cloud container refused to start ("no login")."""
    import uvicorn
    from salescoach import identity
    from salescoach.store import stores
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("salescoach.web.app.create_app", lambda *a, **k: object())
    monkeypatch.setattr(stores, "db_path", lambda: "postgresql://app@db:5432/salescoach")
    monkeypatch.setattr(cli, "serving_role_problems", lambda url: [])            # no database here; tested in test_sec_serve_role
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "a long random string for the tests")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "1234.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "shh")
    monkeypatch.setenv("GOOGLE_ALLOWED_DOMAINS", "tessel.test")
    monkeypatch.setenv("SALESCOACH_TOKEN_KEYS", "k1:" + "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=")
    assert cli.main(["serve", "--role", "web", "--host", "0.0.0.0", "--port", "8140"]) != 2, capsys.readouterr().err
    assert calls[-1]["host"] == "0.0.0.0"
    monkeypatch.setenv(identity.MODE_ENV, "local")                                   # a laptop: still refused
    assert cli.main(["serve", "--role", "web", "--host", "0.0.0.0"]) == 2 and len(calls) == 1


# ---- a PBKDF2 hash instead of the password ---------------------------------------------------------------------

def test_pbkdf2_hash_is_accepted_and_wins_over_the_plain_password(client, monkeypatch, capsys):
    monkeypatch.delenv("SALESCOACH_PASSWORD", raising=False)
    made = hosted.make_hash(PASSWORD, iterations=1000)
    scheme, iterations, salt, digest = made.split("$")
    assert (scheme, iterations, len(salt), len(digest)) == ("pbkdf2_sha256", "1000", 32, 64)
    assert hosted.make_hash(PASSWORD, iterations=1000, salt=salt) == made         # deterministic for a salt
    assert hosted.make_hash(PASSWORD, iterations=1000) != made                     # a fresh salt each time
    monkeypatch.setenv("SALESCOACH_PASSWORD_HASH", made)
    assert hosted.auth_enabled() and hosted.verify_password(PASSWORD) and not hosted.verify_password(PASSWORD + "x")
    assert not hosted.verify_password("") and not hosted.verify_password(None)
    assert login(client, "nope").status_code == 401
    assert login(client).status_code == 303 and client.get("/").status_code == 200
    monkeypatch.setenv("SALESCOACH_PASSWORD", "a plain one that must lose")       # the hash wins
    client.cookies.clear()
    assert login(client, "a plain one that must lose").status_code == 401
    assert login(client).status_code == 303
    for malformed in ("pbkdf2_sha256$x$y", "md5$1$s$h", "pbkdf2_sha256$0$s$h", "pbkdf2_sha256$1$$h", "garbage"):
        monkeypatch.setenv("SALESCOACH_PASSWORD_HASH", malformed)
        assert not hosted.verify_password(PASSWORD), malformed
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(PASSWORD + "\n"))
    assert cli.main(["password-hash"]) is None
    printed = capsys.readouterr().out.strip()
    monkeypatch.setenv("SALESCOACH_PASSWORD_HASH", printed)
    assert printed.startswith("pbkdf2_sha256$600000$") and hosted.verify_password(PASSWORD)
    with pytest.raises(ValueError):
        hosted.make_hash("")


# ---- setup step 5 on a hosted install --------------------------------------------------------------------------

def test_setup_connections_uses_hosted_wording(pclient, db, password):
    client = pclient
    login(client, **PUBLIC_ORIGIN)
    page = {"x-forwarded-for": "203.0.113.9"}
    r = client.get("/setup/connections", headers=page)
    assert r.status_code == 200
    assert r.text.count("Not available in a hosted install") == 5
    assert "callcap/build.sh" not in r.text and "System Settings" not in r.text and "hosted install" in r.text
    assert "How to fix it" not in r.text
    from salescoach.setupui import state
    rows = state.connections(db)
    assert [row["key"] for row in rows] == ["gmail", "calendar", "capture", "speech", "cli"]
    assert all(row["optional"] and not row["ok"] and not row["fix"] for row in rows)
    assert next(s for s in state.steps(db, rows=rows) if s["key"] == "connections")["status"] == "done"
    assert not [m for m in state.missing(db, rows=rows) if m["step"] == "connections"]
    r = client.get("/setup/sources", headers=page)
    assert PUBLIC + HOOK in r.text and "Hosted install" in r.text and "Before you switch this on" not in r.text
    r = client.get("/setup/model", headers=page)
    assert r.status_code == 200 and "not found on this machine" not in r.text


def test_setup_connections_on_localhost_is_unchanged(client, db, local):
    r = client.get("/setup/connections")
    assert r.status_code == 200 and "Not available in a hosted install" not in r.text


def test_a_transcript_uploaded_while_logged_in_lands_under_data_dir(client, db, password, tmp_path):
    from salescoach import config
    login(client)
    r = client.post("/import/file", headers=LOCAL_ORIGIN, files={"file": ("acme.txt", NAMED.encode(), "text/plain")},
                    data={"title": "", "lang_mode": "auto"})
    assert r.status_code == 303 and "Imported" in r.headers["location"]
    kept = list((config.DATA_DIR / "inbox").rglob("*"))
    assert any(p.is_file() for p in kept) and str(config.DATA_DIR).startswith(str(tmp_path))
    assert json.loads(json.dumps({"n": len(kept)}))["n"] >= 1
