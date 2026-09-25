"""Google sign-in and incremental consent (web/auth.py, googleauth.py) against a mocked Google.

Unit level (both backends): the claim checks neither library does (email_verified, hd in the
allowed domains, the address in that domain), the ID-token parse (signature, iss, aud, exp, the
nonce WE issued), the one-shot state store, the authorization URL's parameters, the cloud
preflight. HTTP level (Postgres only, because cloud mode is): the whole flow for an invited user;
wrong or reused state, wrong nonce, wrong hd, unverified address, unknown address (the invite
page), a disabled user, a Google that fails the exchange; the bootstrap admin created once under
two concurrent callbacks; rate limits per address and per email; a disabled user or a revoked
session is out at the next request; log out here and everywhere; the connect flow stores one
grant that grows, refuses another Google account, and disconnect revokes and deletes; local mode
has none of these routes.
"""
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi.testclient import TestClient

from salescoach import cli, googleauth, identity, sessions, users
from salescoach.execution import tokens
from salescoach.store import stores
from salescoach.web import auth
from salescoach.web.app import create_app
from conftest import seed_org_settings

CLIENT_ID = "1234.apps.googleusercontent.com"
DOMAIN = "tessel.test"
ORIGIN = {"origin": "http://testserver"}
K1 = "k1:" + "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="


class FakeGoogle:
    """The three endpoints the app talks to, behind an httpx.MockTransport, plus the ID tokens the
    token endpoint mints for `identity` with the nonce the test copied from the redirect."""

    def __init__(self):
        self.key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "t1"})
        self.jwks = {"keys": [self.key.as_dict(is_private=False)]}
        self.identity = {"sub": "sub-asha", "email": "asha@tessel.test", "email_verified": True, "hd": DOMAIN,
                         "name": "Asha Rao"}
        self.nonce = None
        self.exchanges, self.refreshes, self.revoked = [], [], []
        self.refresh_token = "1//rt-asha"
        self.scope = None                       # None = echo what the feature asks; else this string
        self.fail = None                        # (status, body) to answer the token endpoint with
        self.claims_override = {}

    def id_token(self, nonce=None, **override):
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "iat": now, "exp": now + 3600,
                  "nonce": nonce if nonce is not None else self.nonce, **self.identity, **self.claims_override,
                  **override}
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode({"alg": "RS256", "kid": "t1"}, claims, self.key).decode()

    def handler(self, request: httpx.Request):
        url = str(request.url)
        if url == googleauth.JWKS_URL:
            return httpx.Response(200, json=self.jwks)
        if url == googleauth.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("grant_type") == "refresh_token":
                self.refreshes.append(form)
                return httpx.Response(200, json={"access_token": "ya29.refreshed", "expires_in": 3600})
            self.exchanges.append(form)
            if self.fail:
                return httpx.Response(self.fail[0], json=self.fail[1])
            body = {"access_token": "ya29.access", "expires_in": 3599, "token_type": "Bearer",
                    "id_token": self.id_token()}
            if form.get("code", "").startswith("code-connect"):
                if self.refresh_token:
                    body["refresh_token"] = self.refresh_token
                body["scope"] = self.scope or form.get("code")[len("code-connect:"):]
            return httpx.Response(200, json=body)
        if url == googleauth.REVOKE_URL:
            self.revoked.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": "unexpected " + url})


@pytest.fixture
def fake(monkeypatch):
    google = FakeGoogle()
    monkeypatch.setattr(googleauth, "transport", httpx.MockTransport(google.handler))
    monkeypatch.setattr(googleauth, "independent_verify", lambda id_token: dict(jwt.decode(id_token, google.jwks)))
    monkeypatch.setenv(googleauth.CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(googleauth.CLIENT_SECRET_ENV, "shh")
    monkeypatch.setenv(googleauth.DOMAINS_ENV, DOMAIN)
    monkeypatch.delenv(googleauth.BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(auth, "pending", googleauth.Pending())
    return google


@pytest.fixture
def cloud(monkeypatch, tmp_path, fake, request):
    """Cloud mode with a fresh store (the same isolation the `db` fixture gives, after the mode is set
    so no local user is made) and nothing signed in. The connection handed to the test is the
    OPERATOR's, for arranging and checking: on Postgres the owner role on the test's schema (every row,
    row security bypassed, nobody bound: what `psql` as the owner sees). The app under test connects
    as the app role and runs under every policy."""
    from salescoach import config
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "a long random string for the tests")
    monkeypatch.setenv(tokens.KEYS_ENV, K1)
    for name in ("SALESCOACH_PASSWORD", "SALESCOACH_PASSWORD_HASH", "SALESCOACH_PUBLIC_URL", "SALESCOACH_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SALESCOACH_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(stores, "WORLD_DB", tmp_path / "absent-world.db")
    conn = stores.sales()                   # the app role: makes the test's schema
    if conn.dialect != "postgres":
        yield conn
        conn.close()
        return
    conn.close()
    operator = request.getfixturevalue("pg_owner")
    seed_org_settings(operator)            # the org is set up; who may sign in is what these tests are about
    yield operator


def client_for(**kw):
    app = create_app(start_worker=False, live_factory=None, hub=None, **kw)
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def start(client, nxt=None, **headers):
    """GET /auth/google -> (state, nonce, code_challenge, the full query) from the redirect to Google."""
    r = client.get(auth.GOOGLE_START, params={"next": nxt} if nxt else None, headers=headers)
    assert r.status_code == 303, r.text
    target = urlparse(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == googleauth.AUTH_URL
    q = {k: v[0] for k, v in parse_qs(target.query).items()}
    return q["state"], q["nonce"], q["code_challenge"], q


def sign_in(client, fake, invited=None, nxt=None, **headers):
    state, nonce, _challenge, _q = start(client, nxt, **headers)
    fake.nonce = nonce
    return client.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"}, headers=headers)


def reset_limits(client):
    """Between sections of a refusal test: the per-address limit is real (five failures) and would
    otherwise stop the later sections from being about what they say."""
    from salescoach import hosted
    client.app.state.login_limiter = hosted.LoginLimiter()
    client.app.state.email_limiter = hosted.LoginLimiter()


def invite(conn, email, role="rep", name=""):
    row = users.create(conn, email, name, role=role, status="invited")
    conn.commit()
    return row


# ---- unit level: the checks ----------------------------------------------------------------------

def test_check_claims_does_what_neither_library_does(monkeypatch):
    monkeypatch.setenv(googleauth.DOMAINS_ENV, "tessel.test, Acme.example")
    good = {"sub": "s", "email": "Asha@Tessel.test", "email_verified": True, "hd": "tessel.test", "name": "Asha"}
    assert googleauth.check_claims(good) == {"sub": "s", "email": "asha@tessel.test", "name": "Asha", "hd": "tessel.test"}
    assert googleauth.check_claims({**good, "email_verified": "true"})["email"] == "asha@tessel.test"
    assert googleauth.check_claims({**good, "email": "x@acme.example", "hd": "acme.example"})["hd"] == "acme.example"
    for bad, why in [({**good, "email_verified": False}, "not verified"), ({**good, "email_verified": None}, "not verified"),
                     ({**good, "hd": "gmail.com"}, "work Google account"), ({**good, "hd": None}, "work Google account"),
                     ({**good, "email": "asha@evil.test"}, "not in its Workspace domain"),
                     ({**good, "email": ""}, "did not say"), ({**good, "sub": ""}, "did not say")]:
        with pytest.raises(googleauth.Denied, match=why):
            googleauth.check_claims(bad)
    monkeypatch.delenv(googleauth.DOMAINS_ENV)
    with pytest.raises(googleauth.Denied, match="GOOGLE_ALLOWED_DOMAINS"):
        googleauth.check_claims(good)


def test_parse_id_token_checks_signature_issuer_audience_expiry_and_nonce(fake):
    claims = googleauth.parse_id_token(fake.id_token("n1"), "n1", keys=fake.jwks)
    assert claims["email"] == "asha@tessel.test" and claims["nonce"] == "n1"
    with pytest.raises(googleauth.GoogleError, match="nonce"):
        googleauth.parse_id_token(fake.id_token("n1"), "n2", keys=fake.jwks)
    with pytest.raises(googleauth.GoogleError):
        googleauth.parse_id_token(fake.id_token("n1", aud="other-client"), "n1", keys=fake.jwks)
    with pytest.raises(googleauth.GoogleError):
        googleauth.parse_id_token(fake.id_token("n1", iss="https://evil.example"), "n1", keys=fake.jwks)
    with pytest.raises(googleauth.GoogleError):
        googleauth.parse_id_token(fake.id_token("n1", exp=int(time.time()) - 600), "n1", keys=fake.jwks)
    other = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "t1"})
    forged = jwt.encode({"alg": "RS256", "kid": "t1"}, {"iss": googleauth.ISSUERS[0], "aud": CLIENT_ID,
                        "exp": int(time.time()) + 60, "nonce": "n1", "sub": "s"}, other).decode()
    with pytest.raises(googleauth.GoogleError):
        googleauth.parse_id_token(forged, "n1", keys=fake.jwks)
    with pytest.raises(googleauth.GoogleError):
        googleauth.parse_id_token("not.a.jwt", "n1", keys=fake.jwks)
    assert googleauth.parse_id_token(fake.id_token("n1"), "n1")["sub"] == "sub-asha"       # keys from the mocked JWKS URL


def test_pending_state_is_one_shot_bounded_and_expires():
    store = googleauth.Pending(ttl_s=60, max_items=3)
    state, nonce, challenge = store.begin(kind="signin", next="/deals")
    assert len(state) > 30 and len(nonce) > 30 and challenge and "=" not in challenge
    taken = store.take(state)
    assert taken["nonce"] == nonce and taken["next"] == "/deals" and taken["kind"] == "signin" and "verifier" in taken
    assert store.take(state) is None and store.take(None) is None and store.take("nope") is None
    olds = [store.begin(kind="signin")[0] for _ in range(4)]
    assert store.take(olds[0]) is None and store.take(olds[3])                          # the oldest was dropped
    s2 = store.begin(kind="signin")[0]
    store._items[s2]["at"] -= 61
    assert store.take(s2) is None


def test_authorization_url_carries_pkce_state_nonce_and_the_hd_hint(monkeypatch):
    monkeypatch.setenv(googleauth.CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(googleauth.DOMAINS_ENV, DOMAIN)
    url = googleauth.authorization_url("https://coach.example/auth/callback", googleauth.SIGNIN_SCOPES, "st", "no", "ch")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q == {"client_id": CLIENT_ID, "redirect_uri": "https://coach.example/auth/callback", "response_type": "code",
                 "scope": "openid email profile", "state": "st", "nonce": "no", "code_challenge": "ch",
                 "code_challenge_method": "S256", "hd": DOMAIN}
    url = googleauth.authorization_url("https://coach.example/auth/connect/callback",
                                       (*googleauth.SIGNIN_SCOPES, *googleauth.FEATURE_SCOPES["gmail"]), "st", "no", "ch",
                                       login_hint="asha@tessel.test", offline=True)
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert (q["access_type"], q["prompt"], q["include_granted_scopes"], q["login_hint"]) == (
        "offline", "consent", "true", "asha@tessel.test")
    assert q["scope"].split() == ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.compose",
                                  "https://www.googleapis.com/auth/gmail.readonly"]
    monkeypatch.setenv(googleauth.DOMAINS_ENV, "a.test,b.test")
    q = {k: v[0] for k, v in parse_qs(urlparse(googleauth.authorization_url("u", (), "s", "n", "c")).query).items()}
    assert q["hd"] == "*"
    verifier, challenge = googleauth.new_pkce()
    import base64
    import hashlib
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_cloud_preflight_names_every_missing_setting(monkeypatch):
    for name in ("SALESCOACH_SESSION_SECRET", googleauth.CLIENT_ID_ENV, googleauth.CLIENT_SECRET_ENV,
                 googleauth.DOMAINS_ENV, tokens.KEYS_ENV):
        monkeypatch.delenv(name, raising=False)
    problems = cli.cloud_problems()
    assert [p.split(" ")[0] for p in problems] == ["SALESCOACH_SESSION_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                                                   "GOOGLE_ALLOWED_DOMAINS", "SALESCOACH_TOKEN_KEYS"]
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "s")
    monkeypatch.setenv(googleauth.CLIENT_ID_ENV, "c")
    monkeypatch.setenv(googleauth.CLIENT_SECRET_ENV, "s")
    monkeypatch.setenv(googleauth.DOMAINS_ENV, DOMAIN)
    monkeypatch.setenv(tokens.KEYS_ENV, K1)
    assert cli.cloud_problems() == []


def test_local_mode_has_no_google_routes(db, monkeypatch):
    for name in ("SALESCOACH_PASSWORD", "SALESCOACH_PASSWORD_HASH", "SALESCOACH_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    client = client_for()
    assert client.get(auth.GOOGLE_START).status_code == 404
    assert client.get(auth.GOOGLE_CALLBACK, params={"state": "x", "code": "y"}).status_code == 404
    assert client.get(auth.CONNECT_START, params={"feature": "gmail"}).status_code == 404
    assert client.get("/login").status_code == 303                       # no password: the localhost tool


# ---- HTTP level: cloud mode ------------------------------------------------------------------------

pytestmark_cloud = pytest.mark.postgres_only


@pytestmark_cloud
def test_login_offers_google_and_the_redirect_carries_state_nonce_and_pkce(cloud, fake):
    client = client_for()
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/deals.json").status_code == 401
    page = client.get("/login")
    assert page.status_code == 200 and "Sign in with Google" in page.text and 'name="password"' not in page.text
    assert "Log out" not in page.text
    state, nonce, challenge, q = start(client, nxt="/deals")
    assert q["client_id"] == CLIENT_ID and q["scope"] == "openid email profile" and q["hd"] == DOMAIN
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == "http://testserver/auth/callback"
    assert "access_type" not in q and "prompt" not in q
    assert client.post("/login", data={"password": "x"}, headers=ORIGIN).status_code == 303   # no password to post


@pytestmark_cloud
def test_the_whole_sign_in_for_an_invited_user(cloud, fake):
    invited = invite(cloud, "asha@tessel.test")
    client = client_for()
    r = sign_in(client, fake, nxt="/deals")
    assert r.status_code == 303 and r.headers["location"] == "/deals", r.text
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{sessions.COOKIE}=") and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "asha" not in cookie and "u-" not in cookie                                   # a signed random id only
    [exchange] = fake.exchanges
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "code-1"
    assert exchange["redirect_uri"] == "http://testserver/auth/callback" and len(exchange["code_verifier"]) >= 43
    assert exchange["client_id"] == CLIENT_ID and exchange["client_secret"] == "shh"
    row = users.get(cloud, invited["id"])
    assert (row["status"], row["google_sub"], row["name"], row["role"]) == ("active", "sub-asha", "Asha Rao", "rep")
    [session] = sessions.live_for(cloud, row["id"])
    assert session["ip"] and session["user_agent"]
    events = cloud.execute("SELECT kind, actor, actor_user_id, owner_id FROM events ORDER BY id").fetchall()
    assert [tuple(e) for e in events] == [("user.signin", f"user:{row['id']}", row["id"], row["id"])]
    home = client.get("/", headers={"accept": "text/html"})
    assert home.status_code == 200 and "Log out" in home.text and 'href="/me/setup"' in home.text
    me = client.get("/me/setup")
    assert me.status_code == 200 and "Asha Rao" in me.text
    # the same state cannot be replayed, and the cookie is what carries the session
    assert client.get(auth.GOOGLE_CALLBACK, params={"state": "gone", "code": "code-1"}).status_code == 400
    out = client.post("/logout", headers=ORIGIN)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    assert sessions.live_for(cloud, row["id"]) == []
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


@pytestmark_cloud
def test_callback_refusals(cloud, fake):
    invite(cloud, "asha@tessel.test")
    client = client_for()
    # a state we never issued
    r = client.get(auth.GOOGLE_CALLBACK, params={"state": "forged", "code": "c"})
    assert r.status_code == 400 and auth.EXPIRED_LINK in r.text and fake.exchanges == []
    # a reused state
    state, nonce, _c, _q = start(client)
    fake.nonce = nonce
    assert client.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"}).status_code == 303
    assert client.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"}).status_code == 400
    client = client_for()
    # the ID token carries another nonce
    state, nonce, _c, _q = start(client)
    fake.nonce = "some-other-nonce"
    r = client.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"})
    assert r.status_code == 502 and "did not verify" in r.text and "set-cookie" not in r.headers
    # the two verifications disagree
    fake.nonce = None
    reset_limits(client)
    state, nonce, _c, _q = start(client)
    fake.nonce = nonce
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(googleauth, "independent_verify", lambda id_token: {"sub": "someone-else"})
        r = client.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"})
    assert r.status_code == 502 and "disagree" in r.text
    # wrong hd, unverified address, an address outside the domain
    for override, expect in [({"hd": "gmail.com"}, "work Google account"), ({"email_verified": False}, "not verified"),
                             ({"email": "asha@evil.test"}, "Workspace domain")]:
        fake.claims_override = override
        reset_limits(client)
        r = sign_in(client, fake)
        assert r.status_code == 403 and expect in r.text and "not on the list" in r.text, override
    fake.claims_override = {}
    # unknown address: the invite page; then a disabled user
    fake.identity["email"], fake.identity["sub"] = "nobody@tessel.test", "sub-nobody"
    reset_limits(client)
    r = sign_in(client, fake)
    assert r.status_code == 403 and "Ask your admin for an invite" in r.text and users.by_email(cloud, "nobody@tessel.test") is None
    assert cloud.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    fake.identity["email"], fake.identity["sub"] = "asha@tessel.test", "sub-asha"
    users.update(cloud, users.by_email(cloud, "asha@tessel.test")["id"], status="disabled")
    cloud.commit()
    reset_limits(client)
    r = sign_in(client, fake)
    assert r.status_code == 403 and auth.DISABLED in r.text
    # Google fails the exchange
    users.update(cloud, users.by_email(cloud, "asha@tessel.test")["id"], status="active")
    cloud.commit()
    fake.fail = (400, {"error": "invalid_grant"})
    reset_limits(client)
    r = sign_in(client, fake)
    assert r.status_code == 502 and "invalid_grant" in r.text
    # the browser came back with an error instead of a code
    fake.fail = None
    reset_limits(client)
    state, nonce, _c, _q = start(client)
    r = client.get(auth.GOOGLE_CALLBACK, params={"state": state, "error": "access_denied"})
    assert r.status_code == 400 and "access_denied" in r.text


@pytestmark_cloud
def test_bootstrap_admin_is_created_once_under_two_concurrent_callbacks(cloud, fake, monkeypatch):
    monkeypatch.setenv(googleauth.BOOTSTRAP_ENV, "Admin@Tessel.test")
    fake.identity = {"sub": "sub-admin", "email": "admin@tessel.test", "email_verified": True, "hd": DOMAIN,
                     "name": "Adi Admin"}
    app = create_app(start_worker=False, live_factory=None, hub=None)
    starts = []
    for _ in range(2):
        c = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
        starts.append((c, *start(c)[:2]))
    barrier, results, errors = threading.Barrier(2), [], []

    def go(client, state, nonce):
        try:
            fake.nonce = nonce                        # both tokens carry their own nonce: the fake mints per call
            barrier.wait(timeout=10)
            results.append(client.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"}).status_code)
        except Exception as exc:                      # pragma: no cover - reported below
            errors.append(repr(exc))

    # The fake mints the ID token at exchange time from fake.nonce; make it mint each thread's own nonce.
    minted = {s: n for _c, s, n in starts}
    original = fake.handler

    def per_state(request):
        if str(request.url) == googleauth.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            fake.nonce = minted[form["code_verifier"]] if form["code_verifier"] in minted else fake.nonce
        return original(request)

    verifiers = {}
    for c, s, n in starts:                            # map each state's verifier to its nonce
        verifiers[auth.pending._items[s]["verifier"]] = n
    minted = verifiers

    monkeypatch.setattr(googleauth, "transport", httpx.MockTransport(per_state))
    threads = [threading.Thread(target=go, args=(c, s, n)) for c, s, n in starts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == [] and results == [303, 303]
    rows = users.list_users(cloud)
    assert len(rows) == 1 and rows[0]["email"] == "admin@tessel.test" and rows[0]["role"] == "admin"
    assert rows[0]["status"] == "active" and rows[0]["google_sub"] == "sub-admin"
    assert len(sessions.live_for(cloud, rows[0]["id"])) == 2
    kinds = [r[0] for r in cloud.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds.count("user.bootstrap") == 1 and kinds.count("user.signin") == 2
    # once created, the bootstrap address is an ordinary user: disabling it disables it
    users.update(cloud, rows[0]["id"], status="disabled")
    cloud.commit()
    c = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    assert sign_in(c, fake).status_code == 403


@pytestmark_cloud
def test_rate_limits_per_address_and_per_email(cloud, fake, monkeypatch):
    public = "https://coach.example.test"
    monkeypatch.setenv("SALESCOACH_PUBLIC_URL", public)
    app = create_app(start_worker=False, live_factory=None, hub=None, trusted_origins=set())
    client = TestClient(app, base_url=public, follow_redirects=False, raise_server_exceptions=False)

    def attempt(ip, email):
        fake.identity["email"], fake.identity["sub"] = email, "sub-" + email
        headers = {"host": "coach.example.test", "x-forwarded-for": ip, "x-forwarded-proto": "https"}
        return sign_in(client, fake, **headers)

    # five refused sign-ins from one address (five different unknown people) lock that address ...
    for i in range(5):
        assert attempt("203.0.113.9", f"nobody{i}@tessel.test").status_code == 403
    r = client.get(auth.GOOGLE_START, headers={"host": "coach.example.test", "x-forwarded-for": "203.0.113.9"})
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert start(client, host="coach.example.test", **{"x-forwarded-for": "198.51.100.1"})   # ... not another one
    # five refusals for one address-of-a-person from five client addresses lock that email ...
    for i in range(5):
        assert attempt(f"198.51.100.{10 + i}", "nobody@tessel.test").status_code == 403
    r = attempt("198.51.100.99", "nobody@tessel.test")
    assert r.status_code == 429 and "Too many attempts" in r.text
    # ... while another person from that same fresh address still reaches the invite page
    assert attempt("198.51.100.99", "someone@tessel.test").status_code == 403
    assert app.state.email_limiter.retry_after("nobody@tessel.test") > 0
    assert app.state.login_limiter.retry_after("198.51.100.99") == 0
    # three refusals for a not-yet-invited person, then the invite, then a success clears her counter
    for i in range(3):
        assert attempt(f"198.51.100.{30 + i}", "asha@tessel.test").status_code == 403
    assert app.state.email_limiter._failures.get("asha@tessel.test")
    invite(cloud, "asha@tessel.test")
    assert attempt("198.51.100.40", "asha@tessel.test").status_code == 303
    assert app.state.email_limiter._failures.get("asha@tessel.test") is None


@pytestmark_cloud
def test_disabled_user_and_revoked_session_are_out_at_the_next_request(cloud, fake):
    row = invite(cloud, "asha@tessel.test")
    client = client_for()
    assert sign_in(client, fake).status_code == 303
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200
    users.update(cloud, row["id"], status="disabled")
    cloud.commit()
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/deals.json").status_code == 401
    users.update(cloud, row["id"], status="active")
    cloud.commit()
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200          # the session itself lived
    users.update(cloud, row["id"], role="manager")
    cloud.commit()
    with identity.activate(None):
        pass
    assert client.get("/me/setup").status_code == 200
    sessions.revoke_all(cloud, row["id"])
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


@pytestmark_cloud
def test_log_out_everywhere_ends_every_session_of_the_user(cloud, fake):
    row = invite(cloud, "asha@tessel.test")
    laptop, phone = client_for(), client_for()
    assert sign_in(laptop, fake).status_code == 303 and sign_in(phone, fake).status_code == 303
    assert len(sessions.live_for(cloud, row["id"])) == 2
    assert phone.post("/logout", headers=ORIGIN).status_code == 303                     # here only
    assert len(sessions.live_for(cloud, row["id"])) == 1
    assert laptop.get("/", headers={"accept": "text/html"}).status_code == 200
    assert sign_in(phone, fake).status_code == 303
    assert laptop.post("/logout", data={"everywhere": "1"}, headers=ORIGIN).status_code == 303
    assert sessions.live_for(cloud, row["id"]) == []
    assert phone.get("/", headers={"accept": "text/html"}).status_code == 303


@pytestmark_cloud
def test_connect_gmail_then_calendar_keeps_one_grant_that_grows(cloud, fake):
    row = invite(cloud, "asha@tessel.test")
    client = client_for()
    assert sign_in(client, fake).status_code == 303
    assert client.get(auth.CONNECT_START, params={"feature": "slack"}).headers["location"].startswith("/me/setup?err=")

    def connect(feature):
        r = client.get(auth.CONNECT_START, params={"feature": feature})
        assert r.status_code == 303
        q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
        assert (q["access_type"], q["prompt"], q["include_granted_scopes"]) == ("offline", "consent", "true")
        assert q["login_hint"] == "asha@tessel.test" and q["redirect_uri"] == "http://testserver/auth/connect/callback"
        assert set(googleauth.FEATURE_SCOPES[feature]) <= set(q["scope"].split()) and "openid" in q["scope"]
        fake.nonce = q["nonce"]
        return client.get(auth.CONNECT_CALLBACK, params={"state": q["state"], "code": "code-connect:" + q["scope"]}), q

    r, q = connect("gmail")
    assert r.status_code == 303 and "Gmail+connected+as+asha%40tessel.test" in r.headers["location"], r.headers
    grant = tokens.get(cloud, row["id"])
    assert grant["status"] == "active" and grant["email"] == "asha@tessel.test"
    assert set(grant["scopes"]) >= set(googleauth.FEATURE_SCOPES["gmail"]) and grant["key_id"] == "k1"
    assert tokens.refresh_token_of(cloud, row["id"]) == "1//rt-asha" and tokens.status_of(cloud, row["id"])["features"] == ["gmail"]
    fake.refresh_token = "1//rt-asha-2"
    r, q = connect("calendar")
    assert r.status_code == 303 and "Calendar+connected" in r.headers["location"]
    assert cloud.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 1
    assert tokens.status_of(cloud, row["id"])["features"] == ["gmail", "calendar"]
    assert tokens.refresh_token_of(cloud, row["id"]) == "1//rt-asha-2"
    kinds = [r[0] for r in cloud.execute("SELECT kind FROM events WHERE kind='google.connect'")]
    assert kinds == ["google.connect", "google.connect"]
    page = client.get("/me/setup")
    assert "Connected as asha@tessel.test" in page.text and "Disconnect" in page.text
    # a needs_reconsent row shows the Reconnect button; a fresh consent heals it
    tokens.mark(cloud, row["id"], "needs_reconsent", "invalid_grant")
    cloud.commit()
    page = client.get("/me/setup")
    assert "Needs reconnect" in page.text and "Reconnect" in page.text
    r, q = connect("gmail")
    assert r.status_code == 303 and tokens.status_of(cloud, row["id"])["status"] == "active"
    # disconnect revokes at Google and deletes the row
    r = client.post("/me/connections/google/disconnect", headers=ORIGIN)
    assert r.status_code == 303 and fake.revoked == [{"token": "1//rt-asha-2"}]
    assert tokens.get(cloud, row["id"]) is None and "Not connected" in client.get("/me/setup").text


@pytestmark_cloud
def test_connect_refuses_another_account_a_partial_grant_and_no_refresh_token(cloud, fake):
    row = invite(cloud, "asha@tessel.test")
    client = client_for()
    assert sign_in(client, fake).status_code == 303

    def connect(feature, code="code-connect:"):
        r = client.get(auth.CONNECT_START, params={"feature": feature})
        q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
        fake.nonce = q["nonce"]
        return client.get(auth.CONNECT_CALLBACK, params={"state": q["state"], "code": code + q["scope"]})

    fake.identity = {**fake.identity, "email": "other@tessel.test", "sub": "sub-other"}
    r = connect("gmail")
    assert "consent+with+your+own+account" in r.headers["location"] and tokens.get(cloud, row["id"]) is None
    fake.identity = {**fake.identity, "email": "asha@tessel.test", "sub": "sub-asha"}
    fake.scope = "openid email profile https://www.googleapis.com/auth/gmail.readonly"       # one box unticked
    r = connect("gmail")
    assert "did+not+grant+everything" in r.headers["location"] and tokens.get(cloud, row["id"]) is None
    fake.scope = None
    fake.refresh_token = None
    r = connect("gmail")
    assert "no+refresh+token" in r.headers["location"] and tokens.get(cloud, row["id"]) is None
    # a stale or foreign state
    r = client.get(auth.CONNECT_CALLBACK, params={"state": "nope", "code": "c"})
    assert r.status_code == 303 and "expired" in r.headers["location"]
    # and the connect routes need a session
    assert client_for().get(auth.CONNECT_START, params={"feature": "gmail"}, headers={"accept": "text/html"}).status_code == 303
