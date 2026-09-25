"""Server-side sessions (salescoach/sessions.py).

Pinned: the cookie is a signed random id and nothing else; a tampered or re-signed cookie names
no session; a session resolves until revoked or expired; use slides the expiry (at most once per
five minutes); revoke_all ends every session of a user except the one it is told to keep; purge
drops the dead ones; cloud mode refuses to sign anything without an explicit secret; the table holds
sha256(session id), never the id itself, so a copy of it names no cookie.
"""
from datetime import datetime, timedelta, timezone

import pytest

from salescoach import identity, sessions

T0 = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "a long random string for the tests")
    monkeypatch.delenv("SALESCOACH_PASSWORD", raising=False)


def test_cookie_holds_only_a_signed_random_id(db, secret):
    sid, cookie = sessions.create(db, "local", ip="203.0.113.9", user_agent="Mozilla/5.0", at=T0)
    assert cookie == f"{sid}.{sessions._sign(sid)}" and "local" not in cookie and len(sid) >= 40
    assert sessions.session_id_from_cookie(cookie) == sid
    row = db.execute("SELECT * FROM sessions WHERE id=?", (sessions.key(sid),)).fetchone()
    assert row["user_id"] == "local" and row["ip"] == "203.0.113.9" and row["user_agent"] == "Mozilla/5.0"
    assert row["expires_at"] == (T0 + timedelta(days=30)).isoformat(timespec="seconds")
    for bad in (None, "", sid, cookie[:-1] + ("0" if cookie[-1] != "0" else "1"), f"other.{sessions._sign(sid)}",
                cookie + ".x", "a.b.c"):
        assert sessions.session_id_from_cookie(bad) is None, bad


def test_the_table_holds_the_hash_of_the_id_never_the_id(db, secret):
    """A leaked sessions table cannot be replayed: no column of any row holds a raw id, the id column is
    sha256 of it, and resolve / revoke / revoke_all / live_for all still work from the raw id."""
    import hashlib
    sid, cookie = sessions.create(db, "local", at=T0)
    other, _ = sessions.create(db, "local", at=T0)
    rows = [dict(r) for r in db.execute("SELECT * FROM sessions").fetchall()]
    assert {r["id"] for r in rows} == {hashlib.sha256(x.encode()).hexdigest() for x in (sid, other)}
    for r in rows:
        assert not any(isinstance(v, str) and (sid in v or other in v) for v in r.values()), r
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (sid,)).fetchone()[0] == 0
    assert sessions.resolve(db, sessions.key(sid), at=T0) is None          # the hash is not a session id
    assert sessions.resolve(db, sessions.session_id_from_cookie(cookie), at=T0)["user_id"] == "local"
    assert len(sessions.live_for(db, "local", at=T0)) == 2
    assert sessions.revoke_all(db, "local", keep=sid) == 1
    assert sessions.resolve(db, sid, at=T0) and sessions.resolve(db, other, at=T0) is None
    assert sessions.revoke(db, sid) and sessions.live_for(db, "local", at=T0) == []


def test_a_cookie_signed_with_another_secret_is_nothing(db, secret, monkeypatch):
    sid, cookie = sessions.create(db, "local", at=T0)
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "a different secret")
    assert sessions.session_id_from_cookie(cookie) is None
    assert sessions.resolve(db, sessions.session_id_from_cookie(cookie), at=T0) is None


def test_resolve_revoke_expire_and_sliding(db, secret):
    sid, _ = sessions.create(db, "local", at=T0)
    live = sessions.resolve(db, sid, at=T0 + timedelta(minutes=1))
    assert live and live["user_id"] == "local" and live["last_seen_at"] == T0.isoformat(timespec="seconds")   # no slide yet
    slid = sessions.resolve(db, sid, at=T0 + timedelta(days=20))
    assert slid["last_seen_at"] == (T0 + timedelta(days=20)).isoformat(timespec="seconds")
    assert slid["expires_at"] == (T0 + timedelta(days=50)).isoformat(timespec="seconds")
    assert sessions.resolve(db, sid, at=T0 + timedelta(days=31))                  # alive because it was used (slides to 61)
    assert sessions.resolve(db, sid, at=T0 + timedelta(days=62)) is None            # then it lapsed
    assert sessions.resolve(db, sid, at=T0 + timedelta(days=40)) is None            # and an earlier clock does not revive it
    sid2, _ = sessions.create(db, "local", at=T0)
    assert sessions.revoke(db, sid2) and not sessions.revoke(db, sid2) and not sessions.revoke(db, "nope")
    assert sessions.resolve(db, sid2, at=T0) is None
    assert sessions.resolve(db, None, at=T0) is None and sessions.resolve(db, "nope", at=T0) is None


def test_revoke_all_spares_the_session_pressing_the_button(db, secret):
    a, _ = sessions.create(db, "local", at=T0)
    b, _ = sessions.create(db, "local", at=T0)
    c, _ = sessions.create(db, "local", at=T0)
    assert {s["id"] for s in sessions.live_for(db, "local", at=T0)} == {sessions.key(x) for x in (a, b, c)}
    assert sessions.revoke_all(db, "local", keep=a) == 2
    assert sessions.resolve(db, a, at=T0) and not sessions.resolve(db, b, at=T0) and not sessions.resolve(db, c, at=T0)
    assert sessions.revoke_all(db, "local") == 1 and sessions.live_for(db, "local", at=T0) == []
    assert sessions.revoke_all(db, "local") == 0


def test_purge_drops_expired_and_long_revoked_sessions(db, secret):
    old, _ = sessions.create(db, "local", at=T0 - timedelta(days=60))
    gone, _ = sessions.create(db, "local", at=T0)
    keep, _ = sessions.create(db, "local", at=T0)
    sessions.revoke(db, gone)
    db.execute("UPDATE sessions SET revoked_at=? WHERE id=?", ((T0 - timedelta(days=10)).isoformat(), sessions.key(gone)))
    assert sessions.purge(db, at=T0) == 2
    assert {r[0] for r in db.execute("SELECT id FROM sessions")} == {sessions.key(keep)}


def test_cloud_mode_needs_an_explicit_secret(monkeypatch):
    monkeypatch.delenv("SALESCOACH_SESSION_SECRET", raising=False)
    monkeypatch.setenv("SALESCOACH_PASSWORD", "pw")
    assert sessions.secret()                                               # local: derived from the password, as before
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    with pytest.raises(sessions.NoSecret, match="SALESCOACH_SESSION_SECRET"):
        sessions.secret()
    assert sessions.session_id_from_cookie("abc.def") is None              # never raises on a request
    monkeypatch.setenv("SALESCOACH_SESSION_SECRET", "set now")
    assert sessions.secret() == b"set now"
