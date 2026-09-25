"""Encrypted OAuth grants (execution/tokens.py) and the Google token endpoint calls behind them.

Pinned: the key ring's format and its refusals; a round trip; a tampered ciphertext, a ciphertext
moved to another row and a key not in the ring are all refused; a row encrypted under an old kid
still decrypts and `rotate` moves it to the newest key; store() keeps exactly one row per (user,
provider) and grows the scope set; a fresh cached access token is reused, an expired one is
refreshed through Google (mocked), invalid_grant marks needs_reconsent once and never loops;
disconnect revokes at Google and deletes the row; revoke_all_for_user marks rows revoked.
"""
import base64
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from salescoach import googleauth
from salescoach.execution import tokens

K1 = "k1:" + base64.b64encode(b"\x01" * 32).decode()
K2 = "k2:" + base64.b64encode(b"\x02" * 32).decode()


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv(tokens.KEYS_ENV, K1)
    return K1


@pytest.fixture
def google(monkeypatch):
    """A mocked Google token/revoke endpoint that records what it was asked and answers as told."""
    calls = {"token": [], "revoke": [], "answer": {"access_token": "at-1", "expires_in": 3600, "token_type": "Bearer"},
             "status": 200}

    def handler(request: httpx.Request):
        if request.url == httpx.URL(googleauth.TOKEN_URL):
            calls["token"].append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(calls["status"], json=calls["answer"])
        if request.url == httpx.URL(googleauth.REVOKE_URL):
            calls["revoke"].append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    monkeypatch.setattr(googleauth, "transport", httpx.MockTransport(handler))
    monkeypatch.setenv(googleauth.CLIENT_ID_ENV, "cid.apps.googleusercontent.com")
    monkeypatch.setenv(googleauth.CLIENT_SECRET_ENV, "csecret")
    return calls


# ---- the key ring ---------------------------------------------------------------------------

def test_key_ring_format_and_refusals(monkeypatch):
    monkeypatch.delenv(tokens.KEYS_ENV, raising=False)
    with pytest.raises(tokens.NoKeys, match="not set"):
        tokens.key_ring()
    assert not tokens.keys_configured()
    monkeypatch.setenv(tokens.KEYS_ENV, f"{K2}, {K1}")
    assert [k for k, _ in tokens.key_ring()] == ["k2", "k1"] and tokens.keys_configured()
    for bad, why in [("k1", "kid:base64key"), ("k1:not-base64!", "not base64"),
                     ("k1:" + base64.b64encode(b"x" * 16).decode(), "32 bytes"), (f"{K1},{K1}", "twice")]:
        monkeypatch.setenv(tokens.KEYS_ENV, bad)
        with pytest.raises(tokens.NoKeys, match=why):
            tokens.key_ring()
    line = tokens.new_key_line()
    kid, material = line.split(":")
    assert kid.startswith("k") and len(base64.b64decode(material)) == 32
    assert tokens.new_key_line() != line


def test_round_trip_tamper_wrong_row_and_missing_key(keys, monkeypatch):
    aad = tokens._aad("u-1", "google", "refresh_token")
    enc, kid = tokens.encrypt("1//refresh-secret", aad)
    assert kid == "k1" and "refresh-secret" not in enc and enc != tokens.encrypt("1//refresh-secret", aad)[0]
    assert tokens.decrypt(enc, kid, aad) == "1//refresh-secret"
    blob = bytearray(base64.b64decode(enc))
    blob[-1] ^= 1
    with pytest.raises(tokens.TokenError, match="tampered"):
        tokens.decrypt(base64.b64encode(bytes(blob)).decode(), kid, aad)
    with pytest.raises(tokens.TokenError):
        tokens.decrypt(enc, kid, tokens._aad("u-2", "google", "refresh_token"))     # moved to another row
    with pytest.raises(tokens.TokenError):
        tokens.decrypt("AAAA", kid, aad)
    monkeypatch.setenv(tokens.KEYS_ENV, K2)
    with pytest.raises(tokens.TokenError, match="no key 'k1'"):
        tokens.decrypt(enc, "k1", aad)


def test_an_old_kid_still_decrypts_and_rotate_moves_rows_to_the_newest(db, keys, monkeypatch):
    tokens.store(db, "local", "1//old", googleauth.FEATURE_SCOPES["gmail"], "maya@tessel.test",
                 access_token="at-old", expires_in=3600)
    db.commit()
    assert tokens.get(db, "local")["key_id"] == "k1"
    monkeypatch.setenv(tokens.KEYS_ENV, f"{K2},{K1}")                  # k2 is new; k1 still listed
    assert tokens.refresh_token_of(db, "local") == "1//old"           # readable under the old kid
    report = tokens.rotate(db)
    assert report == {"rotated": 1, "skipped": 0, "unreadable": [], "key": "k2"}
    row = tokens.get(db, "local")
    assert row["key_id"] == "k2" and tokens.refresh_token_of(db, "local") == "1//old"
    assert tokens.decrypt(row["access_token_enc"], "k2", tokens._aad("local", "google", "access_token")) == "at-old"
    assert tokens.rotate(db)["skipped"] == 1
    monkeypatch.setenv(tokens.KEYS_ENV, K2)                            # k1 dropped: the row is fine on k2
    assert tokens.refresh_token_of(db, "local") == "1//old"
    db.execute("UPDATE oauth_tokens SET key_id='k0' WHERE user_id='local'")
    assert tokens.rotate(db)["unreadable"] == ["local/google (key k0)"]


# ---- rows ----------------------------------------------------------------------------------

def test_store_keeps_one_row_per_user_and_provider_and_grows_the_scopes(db, keys):
    first = tokens.store(db, "local", "1//rt-1", ["openid", *googleauth.FEATURE_SCOPES["gmail"]], "maya@tessel.test")
    assert first["status"] == "active" and set(first["scopes"]) == {"openid", *googleauth.FEATURE_SCOPES["gmail"]}
    assert tokens.status_of(db, "local")["features"] == ["gmail"]
    assert tokens.has_feature(db, "local", "gmail") and not tokens.has_feature(db, "local", "calendar")
    tokens.mark(db, "local", "needs_reconsent", "invalid_grant")
    assert tokens.status_of(db, "local")["status"] == "needs_reconsent"
    again = tokens.store(db, "local", "1//rt-2", googleauth.FEATURE_SCOPES["calendar"], None)
    assert again["status"] == "active" and again["last_error"] is None and again["email"] == "maya@tessel.test"
    assert set(again["scopes"]) == {"openid", *googleauth.FEATURE_SCOPES["gmail"], *googleauth.FEATURE_SCOPES["calendar"]}
    assert tokens.status_of(db, "local")["features"] == ["gmail", "calendar"]
    assert db.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 1
    assert tokens.refresh_token_of(db, "local") == "1//rt-2"           # the newest token replaced the old one
    kept = tokens.store(db, "local", None, [], None)                   # no token in the answer: the stored one stays
    assert tokens.refresh_token_of(db, "local") == "1//rt-2" and kept["email"] == "maya@tessel.test"
    tokens.delete(db, "local")
    with pytest.raises(tokens.TokenError, match="no refresh token"):
        tokens.store(db, "local", None, [], None)
    assert tokens.status_of(db, "local")["status"] == "not_connected"
    with pytest.raises(tokens.NoToken):
        tokens.refresh_token_of(db, "local")


def test_nothing_stored_is_plaintext(db, keys):
    tokens.store(db, "local", "1//rt-plain", [], "maya@tessel.test", access_token="ya29.plain", expires_in=3600)
    row = db.execute("SELECT * FROM oauth_tokens").fetchone()
    dump = json.dumps(dict(row))
    assert "rt-plain" not in dump and "ya29" not in dump and row["key_id"] == "k1"


# ---- using a grant --------------------------------------------------------------------------

def test_access_token_uses_the_cache_then_refreshes_through_google(db, keys, google):
    tokens.store(db, "local", "1//rt", [], "maya@tessel.test", access_token="at-cached", expires_in=3600)
    assert tokens.access_token(db, "local") == "at-cached" and google["token"] == []
    stale = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="seconds")
    db.execute("UPDATE oauth_tokens SET expires_at=?", (stale,))
    db.commit()
    assert tokens.access_token(db, "local") == "at-1"
    [asked] = google["token"]
    assert asked["grant_type"] == "refresh_token" and asked["refresh_token"] == "1//rt"
    assert asked["client_id"] == "cid.apps.googleusercontent.com" and asked["client_secret"] == "csecret"
    row = tokens.get(db, "local")
    assert row["status"] == "active" and row["expires_at"] > stale
    assert tokens.access_token(db, "local") == "at-1" and len(google["token"]) == 1       # cached again


def test_invalid_grant_marks_needs_reconsent_once_and_never_loops(db, keys, google):
    tokens.store(db, "local", "1//dead", [], "maya@tessel.test")
    google["status"], google["answer"] = 400, {"error": "invalid_grant", "error_description": "Token has been revoked."}
    with pytest.raises(tokens.NeedsReconsent, match="connect Google again"):
        tokens.access_token(db, "local")
    row = tokens.get(db, "local")
    assert row["status"] == "needs_reconsent" and "invalid_grant" in row["last_error"]
    with pytest.raises(tokens.NeedsReconsent):
        tokens.access_token(db, "local")
    with pytest.raises(tokens.NeedsReconsent):
        tokens.refresh_token_of(db, "local")
    assert len(google["token"]) == 1                                   # the dead grant is not retried
    assert tokens.status_of(db, "local")["status"] == "needs_reconsent"


def test_other_google_errors_are_reported_not_marked(db, keys, google):
    tokens.store(db, "local", "1//rt", [], "maya@tessel.test")
    google["status"], google["answer"] = 503, {"error": "backend"}
    with pytest.raises(tokens.TokenError, match="backend"):
        tokens.access_token(db, "local")
    row = tokens.get(db, "local")
    assert row["status"] == "active" and "backend" in row["last_error"]
    google["status"], google["answer"] = 200, {"access_token": "at-2", "expires_in": 100}
    assert tokens.access_token(db, "local") == "at-2" and tokens.get(db, "local")["last_error"] is None


def test_disconnect_revokes_at_google_and_deletes(db, keys, google):
    assert tokens.disconnect(db, "local") == {"had": False, "revoked": False}
    tokens.store(db, "local", "1//rt", googleauth.FEATURE_SCOPES["gmail"], "maya@tessel.test")
    assert tokens.disconnect(db, "local") == {"had": True, "revoked": True}
    assert google["revoke"] == [{"token": "1//rt"}]
    assert tokens.get(db, "local") is None


def test_revoke_all_for_user_marks_rows_revoked(db, keys, google):
    tokens.store(db, "local", "1//rt", googleauth.FEATURE_SCOPES["gmail"], "maya@tessel.test",
                 access_token="at", expires_in=3600)
    assert tokens.revoke_all_for_user(db, "local") == 1
    row = tokens.get(db, "local")
    assert row["status"] == "revoked" and row["access_token_enc"] is None and google["revoke"] == [{"token": "1//rt"}]
    with pytest.raises(tokens.NeedsReconsent):
        tokens.access_token(db, "local")
    assert tokens.revoke_all_for_user(db, "local") == 1 and len(google["revoke"]) == 1     # not revoked twice


def test_missing_keys_refuse_before_any_row_is_written(db, monkeypatch):
    monkeypatch.delenv(tokens.KEYS_ENV, raising=False)
    with pytest.raises(tokens.NoKeys):
        tokens.store(db, "local", "1//rt", [], None)
    assert db.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0
    assert os.environ.get(tokens.KEYS_ENV) is None
