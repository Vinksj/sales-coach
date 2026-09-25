"""The end-to-end harness's endpoint overrides (salescoach/endpoints.py).

Unset: every Google and recorder URL is the real one, exactly as before. Set without SALESCOACH_E2E=1: every
use raises OverrideRefused and `salescoach serve` refuses to start. Set with it: each module calls the
overridden URL, and an ID token from the overridden issuer is still verified (signature against the keys at
the overridden URL, audience, issuer): a token signed by any other key, or for another audience, is refused.
"""
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt

from salescoach import cli, endpoints, googleauth
from salescoach.automation import gcal
from salescoach.execution import gmail
from salescoach.sources.recorders import fathom, fireflies

FAKE = "http://fakes:9000"
CLIENT_ID = "e2e.apps.googleusercontent.com"


@pytest.fixture
def clean(monkeypatch):
    for env in (*endpoints.ALL, endpoints.E2E_ENV):
        monkeypatch.delenv(env, raising=False)
    return monkeypatch


def _all_urls():
    return {"auth": googleauth.auth_url(), "token": googleauth.token_url(), "revoke": googleauth.revoke_url(),
            "jwks": googleauth.jwks_url(), "certs": googleauth.certs_url(), "calendar": gcal.events_url(),
            "fireflies": fireflies.url(), "fathom": fathom.base_url()}


def test_unset_means_the_real_urls(clean):
    assert _all_urls() == {"auth": googleauth.AUTH_URL, "token": googleauth.TOKEN_URL,
                           "revoke": googleauth.REVOKE_URL, "jwks": googleauth.JWKS_URL, "certs": None,
                           "calendar": gcal.EVENTS_URL, "fireflies": fireflies.URL, "fathom": fathom.BASE_URL}
    assert googleauth.AUTH_URL.startswith("https://accounts.google.com/")
    assert fireflies.URL == "https://api.fireflies.ai/graphql"
    assert fathom.BASE_URL == "https://api.fathom.ai/external/v1"
    assert endpoints.problems() == []
    svc = gmail.GmailProvider(alias="x", credentials=_creds(), address="a@tessel.test")._svc()
    assert svc._baseUrl == "https://gmail.googleapis.com/"


def _creds():
    from google.oauth2.credentials import Credentials
    return Credentials(token="ya29.test")


@pytest.mark.parametrize("env", endpoints.ALL)
def test_an_override_without_e2e_is_refused_everywhere(clean, env):
    clean.setenv(env, FAKE)
    with pytest.raises(endpoints.OverrideRefused):
        endpoints.override(env)
    assert any(env in p for p in endpoints.problems())
    use = {endpoints.GOOGLE_OAUTH_BASE: googleauth.token_url, endpoints.GOOGLE_API_BASE: gcal.events_url,
           endpoints.FIREFLIES_URL: fireflies.url, endpoints.FATHOM_BASE: fathom.base_url}[env]
    with pytest.raises(endpoints.OverrideRefused):
        use()


def test_serve_refuses_a_stray_override(clean, capsys):
    clean.setenv(endpoints.GOOGLE_OAUTH_BASE, FAKE)
    assert cli.main(["serve", "--role", "web"]) == 2
    assert endpoints.GOOGLE_OAUTH_BASE in capsys.readouterr().err


def test_serve_refuses_even_a_blank_e2e_flag(clean, capsys):
    clean.setenv(endpoints.FIREFLIES_URL, FAKE + "/graphql")
    clean.setenv(endpoints.E2E_ENV, "yes")                 # only exactly "1" counts
    assert cli.main(["serve", "--role", "worker"]) == 2


def test_with_e2e_every_module_calls_the_override(clean):
    clean.setenv(endpoints.E2E_ENV, "1")
    clean.setenv(endpoints.GOOGLE_OAUTH_BASE, FAKE + "/")
    clean.setenv(endpoints.GOOGLE_API_BASE, FAKE)
    clean.setenv(endpoints.FIREFLIES_URL, FAKE + "/fireflies/graphql")
    clean.setenv(endpoints.FATHOM_BASE, FAKE + "/fathom/external/v1")
    assert _all_urls() == {"auth": FAKE + "/o/oauth2/v2/auth", "token": FAKE + "/token", "revoke": FAKE + "/revoke",
                           "jwks": FAKE + "/oauth2/v3/certs", "certs": FAKE + "/oauth2/v1/certs",
                           "calendar": FAKE + "/calendar/v3/calendars/primary/events",
                           "fireflies": FAKE + "/fireflies/graphql", "fathom": FAKE + "/fathom/external/v1"}
    assert endpoints.problems() == []
    assert googleauth.authorization_url("http://x/cb", ["openid"], "s", "n", "c").startswith(FAKE + "/o/oauth2/v2/auth?")
    svc = gmail.GmailProvider(alias="x", credentials=_creds(), address="a@tessel.test")._svc()
    assert svc._baseUrl == FAKE + "/"
    request = svc.users().messages().send(userId="me", body={"raw": "eA"})
    assert request.uri.startswith(FAKE + "/gmail/v1/users/me/messages/send")


def test_recorders_post_to_the_override(clean):
    clean.setenv(endpoints.E2E_ENV, "1")
    clean.setenv(endpoints.FIREFLIES_URL, FAKE + "/fireflies/graphql")
    clean.setenv(endpoints.FATHOM_BASE, FAKE + "/fathom/external/v1")
    seen = []

    def handler(request):
        seen.append(str(request.url).split("?")[0])
        if "fireflies" in str(request.url):
            return httpx.Response(200, json={"data": {"user": {"email": "a@tessel.test", "name": "A"}}})
        return httpx.Response(200, json={"items": []})

    transport = httpx.MockTransport(handler)
    fireflies.FirefliesRecorder("k", "u1", transport=transport).test()
    fathom.FathomRecorder("k", "u1", transport=transport).test()
    assert seen == [FAKE + "/fireflies/graphql", FAKE + "/fathom/external/v1/meetings"]


# ---- the ID token from an overridden issuer is still verified --------------------------------

def _x509(key):
    """A self-signed certificate for `key` (what Google's v1 certs endpoint serves)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    private = key.get_private_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "e2e")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
            .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .sign(private, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def issuer(clean, monkeypatch):
    clean.setenv(endpoints.E2E_ENV, "1")
    clean.setenv(endpoints.GOOGLE_OAUTH_BASE, FAKE)
    clean.setenv(googleauth.CLIENT_ID_ENV, CLIENT_ID)
    clean.setenv(googleauth.DOMAINS_ENV, "tessel.test")
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "e1"})
    fetched = []

    def fetch(request, url):                    # google-auth's own key fetch, at the overridden URL
        fetched.append(url)
        return {"e1": _x509(key)}

    from google.oauth2 import id_token as google_id_token
    monkeypatch.setattr(google_id_token, "_fetch_certs", fetch)
    jwks = {"keys": [key.as_dict(is_private=False)]}
    monkeypatch.setattr(googleauth, "transport", httpx.MockTransport(
        lambda r: httpx.Response(200, json=jwks) if str(r.url) == FAKE + "/oauth2/v3/certs"
        else httpx.Response(404)))

    def token(signer=key, **claims):
        now = int(time.time())
        body = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "iat": now, "exp": now + 600,
                "sub": "s1", "email": "a@tessel.test", "email_verified": True, "hd": "tessel.test",
                "nonce": "n1", **claims}
        return jwt.encode({"alg": "RS256", "kid": "e1"}, body, signer).decode()
    token.fetched = fetched
    return token


def test_overridden_issuer_tokens_verify_with_both_libraries(issuer):
    good = issuer()
    assert googleauth.parse_id_token(good, "n1")["sub"] == "s1"
    assert googleauth.independent_verify(good)["sub"] == "s1"
    assert issuer.fetched == [FAKE + "/oauth2/v1/certs"]


def test_overridden_issuer_still_refuses_bad_tokens(issuer):
    other = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "e1"})
    forged = issuer(signer=other)
    with pytest.raises(googleauth.GoogleError):
        googleauth.parse_id_token(forged, "n1")
    with pytest.raises(ValueError):
        googleauth.independent_verify(forged)
    with pytest.raises(googleauth.GoogleError):
        googleauth.parse_id_token(issuer(), "another nonce")
    with pytest.raises(ValueError):
        googleauth.independent_verify(issuer(aud="someone-else"))
    with pytest.raises(googleauth.GoogleError):
        googleauth.independent_verify(issuer(iss="https://evil.test"))
    with pytest.raises(googleauth.Denied):
        googleauth.check_claims({**googleauth.parse_id_token(issuer(hd="evil.test", email="a@evil.test"), "n1")})
