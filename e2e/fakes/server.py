"""Every external service the cloud stack talks to, faked in one process for the end-to-end run (e2e/).

  Google OIDC      /.well-known/openid-configuration, /o/oauth2/v2/auth (auto-approves as the user named in
                   the `e2e_user` query parameter, else an `e2e_user` cookie, else login_hint), /token
                   (authorization_code with PKCE S256 checked, refresh_token), /revoke, /oauth2/v3/certs
                   (JWKS) and /oauth2/v1/certs (x509, what google-auth fetches). ID tokens are RS256, signed
                   with a key made at start, iss https://accounts.google.com, aud = the client id, the nonce
                   the app sent, hd = the address's domain when it is a Workspace domain.
  Gmail            /gmail/v1/users/me/{profile, messages/send, messages, messages/<id>, drafts, threads}:
                   bearer = an access token this fake issued; every send is recorded with the grant it used.
  Calendar         /calendar/v3/calendars/primary/events (an empty calendar with a sync token)
  Fireflies        /fireflies/graphql     } tests/recorder_fakes.FakeRecorders, one account per API key
  Fathom           /fathom/external/v1/*  }
  Model            /v1/chat/completions, /v1/models: OpenAI-compatible; canned.answer() per schema name;
                   usage per request (rules can make matching requests expensive, for the budget check)
  Control          /_control/*: what the test arranges and inspects (accounts, recorded sends, model calls)

Standard library HTTP server plus what the app image already has (httpx, authlib, cryptography).
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from authlib.jose import JsonWebKey, jwt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("E2E_LIB", "/e2e-lib"))
import canned  # noqa: E402
from recorder_fakes import FakeRecorders  # noqa: E402

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
WORKSPACE = {d.strip().lower() for d in os.environ.get("GOOGLE_ALLOWED_DOMAINS", "").split(",") if d.strip()}
REDIRECT_PREFIX = os.environ.get("E2E_REDIRECT_PREFIX", "")
ISSUER = "https://accounts.google.com"
PUBLIC = os.environ.get("E2E_FAKES_PUBLIC", "http://fakes:9000")
KID = "e2e-1"

LOCK = threading.RLock()
KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": KID})
JWKS = {"keys": [{**KEY.as_dict(is_private=False), "use": "sig", "alg": "RS256"}]}


def _x509() -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    private = KEY.get_private_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "e2e-google")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
            .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=7))
            .sign(private, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


CERTS = {KID: _x509()}

STATE = {
    "names": {},            # email -> display name
    "codes": {},            # code -> {email, nonce, redirect_uri, challenge, scopes, offline}
    "access": {},           # access token -> email
    "refresh": {},          # refresh token -> email
    "granted": {},          # email -> set of scopes granted offline (include_granted_scopes)
    "revoked": [],
    "exchanges": [],        # (grant_type, email)
    "sent": [],             # Gmail sends
    "drafts": [],
    "model_calls": [],
    "model_rules": [],      # [{"system": regex, "prompt_tokens": n, "completion_tokens": n}]
}
RECORDERS = FakeRecorders()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _name(email: str) -> str:
    return STATE["names"].get(email) or email.split("@")[0].title()


def _id_token(email: str, nonce: str) -> str:
    now = int(time.time())
    domain = email.rsplit("@", 1)[-1]
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "azp": CLIENT_ID, "iat": now, "exp": now + 3600,
              "sub": "sub-" + hashlib.sha256(email.encode()).hexdigest()[:16], "email": email,
              "email_verified": True, "name": _name(email), "nonce": nonce}
    if domain in WORKSPACE:
        claims["hd"] = domain
    return jwt.encode({"alg": "RS256", "kid": KID}, claims, KEY).decode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "e2e-fakes"

    def log_message(self, fmt, *args):
        sys.stderr.write("fakes: " + (fmt % args) + "\n")

    # ---- plumbing -----------------------------------------------------------------------------

    def _body(self) -> bytes:
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status: int, body=None, headers=None, raw: bytes = None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _split(self):
        parts = urlsplit(self.path)
        return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}, parse_qs(parts.query)

    def _bearer_email(self):
        token = (self.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        with LOCK:
            return STATE["access"].get(token)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def _route(self, method):
        path, q, multi = self._split()
        try:
            if path == "/_health":
                return self._send(200, {"ok": True})
            if path.startswith("/_control/"):
                return self._control(method, path, q)
            if path == "/.well-known/openid-configuration":
                return self._send(200, {"issuer": ISSUER, "authorization_endpoint": PUBLIC + "/o/oauth2/v2/auth",
                                        "token_endpoint": PUBLIC + "/token", "revocation_endpoint": PUBLIC + "/revoke",
                                        "jwks_uri": PUBLIC + "/oauth2/v3/certs",
                                        "id_token_signing_alg_values_supported": ["RS256"],
                                        "code_challenge_methods_supported": ["S256"]})
            if path == "/o/oauth2/v2/auth" and method == "GET":
                return self._authorize(q)
            if path == "/token" and method == "POST":
                return self._token()
            if path == "/revoke" and method == "POST":
                form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
                with LOCK:
                    STATE["revoked"].append(form.get("token"))
                    known = STATE["refresh"].pop(form.get("token"), None) or STATE["access"].pop(form.get("token"), None)
                return self._send(200 if known else 400, {} if known else {"error": "invalid_token"})
            if path == "/oauth2/v3/certs":
                return self._send(200, JWKS)
            if path == "/oauth2/v1/certs":
                return self._send(200, CERTS)
            if path.startswith("/gmail/v1/"):
                return self._gmail(method, path, q, multi)
            if path.startswith("/calendar/v3/"):
                if not self._bearer_email():
                    return self._send(401, {"error": {"code": 401, "message": "invalid token"}})
                return self._send(200, {"kind": "calendar#events", "items": [], "nextSyncToken": "e2e-sync-1"})
            if path == "/fireflies/graphql" or path.startswith("/fathom/"):
                return self._recorder(method, path)
            if path == "/v1/models":
                return self._send(200, {"object": "list", "data": [{"id": "claude-sonnet-5", "object": "model"},
                                                                   {"id": "claude-opus-5", "object": "model"}]})
            if path == "/v1/chat/completions" and method == "POST":
                return self._chat()
            return self._send(404, {"error": f"fakes: no route {method} {path}"})
        except Exception as exc:                          # a fake bug must show in the logs, not hang the app
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": f"fakes: {type(exc).__name__}: {exc}"})

    # ---- Google sign-in and consent ----------------------------------------------------------

    def _authorize(self, q):
        if q.get("client_id") != CLIENT_ID:
            return self._send(400, {"error": "invalid_client"})
        redirect = q.get("redirect_uri") or ""
        if not REDIRECT_PREFIX or not redirect.startswith(REDIRECT_PREFIX):
            return self._send(400, {"error": "redirect_uri_mismatch", "redirect_uri": redirect})
        if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return self._send(400, {"error": "invalid_request"})
        cookie = dict(p.strip().split("=", 1) for p in (self.headers.get("cookie") or "").split(";") if "=" in p)
        email = (q.get("e2e_user") or cookie.get("e2e_user") or q.get("login_hint") or "").strip().lower()
        if not email:
            return self._send(400, {"error": "no e2e_user: the fake cannot pick an account"})
        if q.get("e2e_deny"):
            return self._send(302, headers={"location": redirect + "?" + urlencode(
                {"error": "access_denied", "state": q.get("state", "")})})
        code = "code-" + secrets.token_urlsafe(16)
        with LOCK:
            STATE["codes"][code] = {"email": email, "nonce": q.get("nonce") or "", "redirect_uri": redirect,
                                    "challenge": q["code_challenge"], "scopes": (q.get("scope") or "").split(),
                                    "offline": q.get("access_type") == "offline",
                                    "include": q.get("include_granted_scopes") == "true"}
        return self._send(302, headers={"location": redirect + "?" + urlencode({"code": code, "state": q.get("state", "")})})

    def _token(self):
        form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return self._send(401, {"error": "invalid_client"})
        grant = form.get("grant_type")
        with LOCK:
            if grant == "authorization_code":
                entry = STATE["codes"].pop(form.get("code") or "", None)
                if entry is None or entry["redirect_uri"] != form.get("redirect_uri"):
                    return self._send(400, {"error": "invalid_grant"})
                if _b64url(hashlib.sha256((form.get("code_verifier") or "").encode()).digest()) != entry["challenge"]:
                    return self._send(400, {"error": "invalid_grant", "error_description": "code_verifier"})
                email = entry["email"]
                STATE["exchanges"].append(("authorization_code", email))
                access = "ya29.e2e-" + secrets.token_urlsafe(12)
                STATE["access"][access] = email
                body = {"access_token": access, "expires_in": 3599, "token_type": "Bearer",
                        "id_token": _id_token(email, entry["nonce"])}
                scopes = set(entry["scopes"])
                if entry["offline"]:
                    granted = STATE["granted"].setdefault(email, set())
                    if entry["include"]:
                        granted |= scopes
                        scopes = set(granted)
                    refresh = "1//e2e-" + secrets.token_urlsafe(12)
                    STATE["refresh"][refresh] = email
                    body["refresh_token"] = refresh
                body["scope"] = " ".join(sorted(scopes))
                return self._send(200, body)
            if grant == "refresh_token":
                email = STATE["refresh"].get(form.get("refresh_token") or "")
                if not email:
                    return self._send(400, {"error": "invalid_grant"})
                STATE["exchanges"].append(("refresh_token", email))
                access = "ya29.e2e-" + secrets.token_urlsafe(12)
                STATE["access"][access] = email
                return self._send(200, {"access_token": access, "expires_in": 3599, "token_type": "Bearer",
                                        "scope": " ".join(sorted(STATE["granted"].get(email, ())))})
        return self._send(400, {"error": "unsupported_grant_type"})

    # ---- Gmail --------------------------------------------------------------------------------

    def _gmail(self, method, path, q, multi):
        email = self._bearer_email()
        if not email:
            return self._send(401, {"error": {"code": 401, "message": "Request had invalid authentication credentials."}})
        rest = path[len("/gmail/v1/users/me"):]
        with LOCK:
            mine = [m for m in STATE["sent"] if m["grant_email"] == email]
            if rest == "/profile":
                return self._send(200, {"emailAddress": email, "messagesTotal": len(mine)})
            if rest == "/messages/send" and method == "POST":
                return self._send(200, self._record(email, json.loads(self._body() or b"{}"), STATE["sent"]))
            if rest == "/drafts" and method == "POST":
                message = self._record(email, (json.loads(self._body() or b"{}")).get("message") or {}, STATE["drafts"])
                return self._send(200, {"id": "d-" + message["id"], "message": message})
            if rest in ("/messages", "/drafts"):
                query = q.get("q") or ""
                found = mine if "in:sent" in query or rest == "/drafts" else []
                m = re.search(r"rfc822msgid:(\S+)", query)
                if m:
                    found = [x for x in found if x["headers"].get("message-id", "").strip("<>") == m.group(1)]
                return self._send(200, {"messages": [{"id": x["id"], "threadId": x["threadId"]} for x in found],
                                        "resultSizeEstimate": len(found)})
            m = re.fullmatch(r"/messages/([^/]+)", rest)
            if m:
                found = next((x for x in STATE["sent"] + STATE["drafts"] if x["id"] == m.group(1)), None)
                if found is None:
                    return self._send(404, {"error": {"code": 404, "message": "Not Found"}})
                wanted = {h.lower() for h in multi.get("metadataHeaders", [])}
                headers = [{"name": k.title() if k != "message-id" else "Message-ID", "value": v}
                           for k, v in found["headers"].items() if not wanted or k in wanted]
                return self._send(200, {"id": found["id"], "threadId": found["threadId"], "labelIds": ["SENT"],
                                        "payload": {"headers": headers}})
            if rest == "/threads":
                return self._send(200, {"threads": [], "resultSizeEstimate": 0})
            m = re.fullmatch(r"/threads/([^/]+)", rest)
            if m:
                msgs = [x for x in mine if x["threadId"] == m.group(1)]
                return self._send(200, {"id": m.group(1), "messages": [
                    {"id": x["id"], "threadId": x["threadId"], "internalDate": str(x["at_ms"]),
                     "payload": {"headers": [{"name": k, "value": v} for k, v in x["headers"].items()]}} for x in msgs]})
        return self._send(404, {"error": {"code": 404, "message": f"fakes: gmail {method} {rest}"}})

    @staticmethod
    def _record(email, body, into):
        raw = base64.urlsafe_b64decode((body.get("raw") or "") + "==")
        parsed = message_from_bytes(raw)
        headers = {k.lower(): str(v) for k, v in parsed.items()}
        n = len(STATE["sent"]) + len(STATE["drafts"]) + 1
        entry = {"id": f"m{n:04d}", "threadId": body.get("threadId") or f"t{n:04d}", "grant_email": email,
                 "headers": headers, "body": parsed.get_payload(decode=True).decode(errors="replace")
                 if not parsed.is_multipart() else "", "at_ms": int(time.time() * 1000)}
        into.append(entry)
        return {"id": entry["id"], "threadId": entry["threadId"], "labelIds": ["SENT"]}

    # ---- recorders ----------------------------------------------------------------------------

    def _recorder(self, method, path):
        if path == "/fireflies/graphql":
            url = "https://api.fireflies.ai/graphql"
        else:
            url = "https://api.fathom.ai" + path[len("/fathom"):]
        query = urlsplit(self.path).query
        request = httpx.Request(method, url + ("?" + query if query else ""),
                                headers={k: v for k, v in self.headers.items()}, content=self._body())
        with LOCK:
            response = RECORDERS.handler(request)
        return self._send(response.status_code, raw=response.content,
                          headers={k: v for k, v in response.headers.items()
                                   if k.lower() in ("retry-after",)})

    # ---- the model ----------------------------------------------------------------------------

    def _chat(self):
        body = json.loads(self._body() or b"{}")
        messages = body.get("messages") or []
        system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
        prompt = next((m.get("content") or "" for m in messages if m.get("role") == "user"), "")
        fmt = body.get("response_format") or {}
        spec = fmt.get("json_schema") or {}
        name, schema = spec.get("name") or "", spec.get("schema") or {}
        prompt_tokens, completion_tokens = 1000, 200
        with LOCK:
            for rule in STATE["model_rules"]:
                if re.search(rule["system"], system):
                    prompt_tokens, completion_tokens = rule["prompt_tokens"], rule["completion_tokens"]
            STATE["model_calls"].append({"schema": name, "model": body.get("model"), "at": time.time(),
                                         "prompt_tokens": prompt_tokens,
                                         "system_head": system[:160], "prompt_len": len(prompt)})
        if fmt.get("type") == "json_schema":
            content = json.dumps(canned.answer(name, schema))
        elif fmt.get("type") == "json_object":
            content = json.dumps({})
        else:
            content = "OK"
        return self._send(200, {"id": "chatcmpl-e2e", "object": "chat.completion", "model": body.get("model"),
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant", "content": content}}],
                                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                                          "total_tokens": prompt_tokens + completion_tokens}})

    # ---- control ------------------------------------------------------------------------------

    def _control(self, method, path, q):
        body = json.loads(self._body() or b"{}") if method == "POST" else {}
        with LOCK:
            if path == "/_control/google_user":
                STATE["names"][body["email"].lower()] = body["name"]
                return self._send(200, {"ok": True})
            if path == "/_control/recorder":
                RECORDERS.account(body["kind"], body["key"], email=body.get("email"), name=body.get("name"),
                                  meetings=body.get("meetings") or [])
                return self._send(200, {"ok": True})
            if path == "/_control/recorder_requests":
                return self._send(200, {"requests": [{"kind": k, "key": key, "method": m, "path": p}
                                                     for k, key, m, p, _ in RECORDERS.requests]})
            if path == "/_control/gmail":
                return self._send(200, {"sent": STATE["sent"], "drafts": STATE["drafts"]})
            if path == "/_control/google":
                return self._send(200, {"exchanges": STATE["exchanges"], "revoked": STATE["revoked"],
                                        "granted": {k: sorted(v) for k, v in STATE["granted"].items()}})
            if path == "/_control/model" and method == "POST":
                STATE["model_rules"] = body.get("rules") or []
                return self._send(200, {"ok": True})
            if path == "/_control/model":
                return self._send(200, {"calls": STATE["model_calls"], "rules": STATE["model_rules"]})
        return self._send(404, {"error": "no such control"})


def main():
    port = int(os.environ.get("PORT", "9000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    sys.stderr.write(f"fakes: listening on {port}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
