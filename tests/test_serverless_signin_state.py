"""Serverless: the sign-in flow's state and its rate limits live in the database, not in one process's memory.

On Vercel consecutive requests reach different instances (and a Render web service may run several processes),
so a callback must find the attempt another instance started, and a limit must count every instance's refusals.

  * googleauth.StorePending keeps googleauth.Pending's contract (one use, ten minutes, a per-address cap that only
    evicts that address's own attempts, a global cap), on both backends;
  * of two callbacks racing with one state, exactly one gets the attempt (Postgres);
  * hosted.StoreLimiter keeps hosted.LoginLimiter's contract, and counts across two apps;
  * HTTP (Postgres, cloud mode): a sign-in started on one app instance completes on another whose process memory
    knows nothing of it, and five refusals on one instance lock the address on the other.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from salescoach import googleauth, hosted
from salescoach.web import auth
from salescoach.web.app import create_app
from test_google_auth import cloud, fake, invite, start  # noqa: F401

pytestmark_cloud = pytest.mark.postgres_only


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_store_pending_is_one_shot_bounded_and_expires(db):
    clock = Clock()
    store = googleauth.StorePending(ttl_s=60, max_items=3, clock=clock)
    state, nonce, challenge = store.begin(kind="signin", next="/deals")
    assert len(state) > 30 and len(nonce) > 30 and challenge and "=" not in challenge
    row = db.execute("SELECT state_hash, data FROM auth_pending").fetchone()
    assert row["state_hash"] != state and state not in row["data"]           # the state itself is never stored
    taken = store.take(state)
    assert taken["nonce"] == nonce and taken["next"] == "/deals" and taken["kind"] == "signin" and "verifier" in taken
    assert store.take(state) is None and store.take(None) is None and store.take("nope") is None
    olds = []
    for _ in range(4):
        clock.t += 1
        olds.append(store.begin(kind="signin")[0])
    assert store.take(olds[0]) is None and store.take(olds[3])                          # the oldest was dropped
    clock.t += 1
    s2 = store.begin(kind="signin")[0]
    clock.t += 61
    assert store.take(s2) is None                                                        # expired


def test_store_pending_caps_live_attempts_per_client_address(db):
    clock = Clock()
    store = googleauth.StorePending(max_items=50, per_client=3, clock=clock)
    good, *_ = store.begin(client="198.51.100.7", kind="signin")
    flood = []
    for _ in range(40):
        clock.t += 1
        flood.append(store.begin(client="203.0.113.9", kind="signin")[0])
    assert store.count("203.0.113.9") == 3
    assert [s for s in flood if store.peek(s)] == flood[-3:]              # the flood evicted its own, oldest first
    assert store.take(good)["client"] == "198.51.100.7"                    # never the other address's attempt


@pytest.mark.parametrize("kind", ["memory", "store"])
def test_store_limiter_keeps_the_login_limiter_contract(db, kind):
    limiter = hosted.LoginLimiter() if kind == "memory" else hosted.StoreLimiter("login_limiter")
    for i in range(5):
        limiter.failed("1.2.3.4", now=1000 + i)
    assert limiter.retry_after("1.2.3.4", now=1010) == hosted.LOGIN_WINDOW_S - 10 + 1
    assert limiter.retry_after("1.2.3.4", now=1000 + hosted.LOGIN_WINDOW_S + 1) == 0    # the window passed
    assert limiter.retry_after("5.6.7.8", now=1010) == 0                                  # another address
    limiter.failed("1.2.3.4", now=2000)
    limiter.succeeded("1.2.3.4")
    assert limiter.retry_after("1.2.3.4", now=2001) == 0


def test_two_store_limiters_share_one_count_and_buckets_stay_apart(db):
    one, two = hosted.StoreLimiter("login_limiter"), hosted.StoreLimiter("login_limiter")
    other = hosted.StoreLimiter("email_limiter")
    for i in range(3):
        one.failed("9.9.9.9", now=5000 + i)
    for i in range(2):
        two.failed("9.9.9.9", now=5003 + i)
    assert one.retry_after("9.9.9.9", now=5010) > 0 and two.retry_after("9.9.9.9", now=5010) > 0
    assert other.retry_after("9.9.9.9", now=5010) == 0
    two.failed("x", now=5000 + 2 * hosted.LOGIN_WINDOW_S)                 # counting prunes rows past the window
    assert db.execute("SELECT COUNT(*) FROM auth_attempts WHERE bucket='login_limiter'").fetchone()[0] == 1


@pytest.mark.postgres_only
def test_two_callbacks_racing_with_one_state_get_it_once(db):
    store = googleauth.StorePending()
    for _ in range(5):
        state, _nonce, _c = store.begin(kind="signin")
        barrier, got = threading.Barrier(2), []

        def take():
            barrier.wait(timeout=10)
            got.append(googleauth.StorePending().take(state))
        threads = [threading.Thread(target=take) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert sorted(x is None for x in got) == [False, True], got


def _instance():
    """A fresh app, as a new serverless instance is: its own app.state, and a process memory with no attempts."""
    return TestClient(create_app(start_worker=False, live_factory=None, hub=None), follow_redirects=False,
                      raise_server_exceptions=False)


@pytestmark_cloud
def test_a_sign_in_started_on_one_instance_completes_on_another(cloud, fake, monkeypatch):
    invite(cloud, "asha@tessel.test")
    one, two = _instance(), _instance()
    state, nonce, _c, _q = start(one)
    binding = one.cookies.get(auth.SIGNIN_COOKIE)
    monkeypatch.setattr(auth, "pending", googleauth.Pending())             # instance two's memory knows nothing
    fake.nonce = nonce
    r = two.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"},    # the same browser, another instance
                headers={"cookie": f"{auth.SIGNIN_COOKIE}={binding}"})
    assert r.status_code == 303 and r.headers["location"] == "/", r.text[:300]
    assert two.get("/me/setup").status_code in (200, 303)
    again = one.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"})
    assert again.status_code == 400 and "expired" in again.text                  # still one use, across instances


@pytestmark_cloud
def test_refusals_on_one_instance_lock_the_address_on_another(cloud, fake):
    one, two = _instance(), _instance()
    fake.identity["email"] = "nobody@tessel.test"
    for i in range(hosted.LOGIN_LIMIT):
        client = one if i % 2 else two
        state, nonce, _c, _q = start(client)
        fake.nonce = nonce
        assert client.get(auth.GOOGLE_CALLBACK, params={"state": state, "code": "code-1"}).status_code == 403
    for client in (one, two):
        r = client.get(auth.GOOGLE_START)
        assert r.status_code == 429 and "Too many attempts" in r.text


@pytest.mark.sqlite_only                     # PRAGMA user_version: the SQLite migration itself
def test_migration_14_adds_the_sign_in_state_tables_to_a_version_13_database(tmp_path, monkeypatch):
    from salescoach.store import stores
    path = tmp_path / "v13.db"
    monkeypatch.setenv("SALES_DB", str(path))
    conn = stores.sales()
    conn.execute("DROP TABLE auth_pending")
    conn.execute("DROP TABLE auth_attempts")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()
    conn = stores.sales()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION == 14
        assert {"auth_pending", "auth_attempts"} <= {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert conn.columns("auth_pending") == ["state_hash", "client", "created", "data"]
    finally:
        conn.close()
