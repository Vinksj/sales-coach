"""Security review, findings 2 and 3: the Google sign-in is bound to the browser that started it, a browser that
is signed in is never switched to another account, and starting sign-ins cannot flood out someone else's.

  * login CSRF: a callback URL minted in the attacker's browser (the state is valid, the code is the attacker's)
    and loaded in the victim's browser signs nobody in and leaves the victim's own session alone; the same URL in
    a fresh browser is refused too (no binding cookie);
  * a browser that started a sign-in and was then signed in as someone else is refused at the callback (409);
  * the binding cookie: HttpOnly, SameSite=Lax, scoped to the callback path, Secure over https, cleared after;
  * starting a sign-in is rate-limited per client address, and googleauth.Pending caps the live attempts per
    address, so one address flooding the store only evicts its own attempts.
HTTP level is Postgres only (cloud mode needs it); the Pending unit test runs on both.
"""
import pytest
from fastapi.testclient import TestClient

from salescoach import googleauth, sessions
from salescoach.web import auth
from test_google_auth import cloud, client_for, fake, invite, start  # noqa: F401

pytestmark_cloud = pytest.mark.postgres_only


def _who(conn, client):
    sid = sessions.session_id_from_cookie(client.cookies.get(sessions.COOKIE))
    row = conn.execute("SELECT user_id FROM sessions WHERE id=?", (sessions.key(sid),)).fetchone() if sid else None
    return row[0] if row else None


def _as(fake, sub, email, name):
    fake.identity = {**fake.identity, "sub": sub, "email": email, "name": name}


@pytestmark_cloud
def test_a_callback_minted_in_another_browser_signs_nobody_in(cloud, fake):
    conn = cloud
    invite(conn, "asha@tessel.test", name="Asha (attacker rep)")
    victim = invite(conn, "mani@tessel.test", role="manager", name="Mani (victim)")
    app = client_for().app
    victim_browser = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _as(fake, "sub-mani", "mani@tessel.test", "Mani")
    st, nonce, _c, _q = start(victim_browser)
    fake.nonce = nonce
    assert victim_browser.get(auth.GOOGLE_CALLBACK, params={"state": st, "code": "code-v"}).status_code == 303
    assert _who(conn, victim_browser) == victim["id"]
    assert victim_browser.cookies.get(auth.SIGNIN_COOKIE) is None           # the binding is spent

    # the attacker starts a sign-in in THEIR browser and stops at Google's redirect back ...
    attacker_browser = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _as(fake, "sub-asha", "asha@tessel.test", "Asha")
    st, nonce, _c, _q = start(attacker_browser)
    fake.nonce = nonce
    exchanges = len(fake.exchanges)
    # ... and gets the victim's browser to load the callback URL: refused, the code is never exchanged
    r = victim_browser.get(auth.GOOGLE_CALLBACK, params={"state": st, "code": "code-attacker"})
    assert r.status_code == 400 and "not started in this browser" in r.text
    assert len(fake.exchanges) == exchanges
    assert _who(conn, victim_browser) == victim["id"]                      # still the victim, not the attacker

    # the same kind of link in a browser that is signed in to nothing: refused as well
    st, nonce, _c, _q = start(attacker_browser)
    fake.nonce = nonce
    fresh = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    r = fresh.get(auth.GOOGLE_CALLBACK, params={"state": st, "code": "code-attacker"})
    assert r.status_code == 400 and _who(conn, fresh) is None and sessions.COOKIE not in r.headers.get("set-cookie", "")


@pytestmark_cloud
def test_a_signed_in_browser_is_never_switched_to_another_account(cloud, fake):
    conn = cloud
    invite(conn, "asha@tessel.test")
    mani = invite(conn, "mani@tessel.test", role="manager")
    conn.execute("UPDATE users SET status='active' WHERE id=?", (mani["id"],))
    conn.commit()
    browser = client_for()
    st, nonce, _c, _q = start(browser)                                     # a sign-in started ...
    fake.nonce = nonce
    _sid, cookie = sessions.create(conn, mani["id"])                       # ... then signed in as Mani in another tab
    browser.cookies.set(sessions.COOKIE, cookie)
    _as(fake, "sub-asha", "asha@tessel.test", "Asha")
    r = browser.get(auth.GOOGLE_CALLBACK, params={"state": st, "code": "code-1"})
    assert r.status_code == 409 and "already signed in" in r.text
    assert _who(conn, browser) == mani["id"]
    assert conn.execute("SELECT status FROM users WHERE email='asha@tessel.test'").fetchone()[0] == "invited"


@pytestmark_cloud
def test_the_binding_cookie_is_httponly_lax_path_scoped_and_secure_over_https(cloud, fake, monkeypatch):
    browser = client_for()
    r = browser.get(auth.GOOGLE_START)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(auth.SIGNIN_COOKIE + "=") and "httponly" in cookie and "samesite=lax" in cookie
    assert f"path={auth.GOOGLE_CALLBACK}" in cookie and "secure" not in cookie   # plain http: not Secure
    monkeypatch.setenv("SALESCOACH_PUBLIC_URL", "https://coach.example.test")
    r = client_for().get(auth.GOOGLE_START)
    assert "secure" in r.headers["set-cookie"].lower()


@pytestmark_cloud
def test_starting_sign_ins_is_rate_limited_per_address(cloud, fake):
    browser = client_for()
    for _ in range(auth.BEGIN_LIMIT):
        assert browser.get(auth.GOOGLE_START).status_code == 303
    r = browser.get(auth.GOOGLE_START)
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert len(auth.pending._items) <= googleauth.PENDING_PER_CLIENT       # and the store holds only the cap


def test_pending_caps_live_attempts_per_client_address():
    store = googleauth.Pending(max_items=50, per_client=3)
    good, *_ = store.begin(client="198.51.100.7", kind="signin")
    flood = [store.begin(client="203.0.113.9", kind="signin")[0] for _ in range(40)]
    assert sum(1 for v in store._items.values() if v["client"] == "203.0.113.9") == 3
    assert [s for s in flood if s in store._items] == flood[-3:]           # the flood evicted its own, oldest first
    assert store.take(good)["client"] == "198.51.100.7"                    # never the other address's attempt
