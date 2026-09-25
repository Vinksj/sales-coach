"""Gmail execution: the only code path that puts mail on the wire.

Two ways to hold a mailbox:
  * local install: the gmail-multi OAuth client (~/.gmail-mcp/credentials.json) and the account's
    refresh token (~/.gmail-mcp/tokens/<alias>.json), READ-ONLY here (the access token is refreshed
    in memory, never written back, so this cannot race or corrupt the MCP server's copy);
  * cloud install: the acting user's own grant (execution/tokens.py, granted on /me/setup with
    gmail.compose + gmail.readonly): GmailProvider.for_user(conn, user_id) refreshes the access
    token through tokens.access_token and never sees a refresh token. provider_for(conn) picks.

No LLM touches this module. It sends exactly the bytes it is handed, and only the policy/approval
layer (execution/policy.py + workflow) may call send().

Recovering an attempt whose outcome is unknown: Gmail may replace a client-supplied Message-ID
(research notes), so a deterministic Message-ID cannot be relied on. Every send carries a custom
X-Salescoach-Key header instead; find_sent_by_key lists Sent since the attempt and matches that
header (Gmail returns custom headers in metadata format even though search cannot query them),
and send() reads the real Message-ID back for threading.

Outbound HTTP goes through google-api-python-client to fixed Google hosts rather than
lib/safefetch: safefetch is GET-only and exists to stop a response from choosing the next URL,
which cannot happen against a fixed API.
"""
import base64
import html
import inspect
import json
import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path
from typing import Optional

GMAIL_DIR = Path(os.path.expanduser("~/.gmail-mcp"))
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]       # the local gmail-multi grant, as it was made
KEY_HEADER = "X-Salescoach-Key"
RECOVERY_LIMIT = 50


class OutgoingEmail:
    def __init__(self, to: list[str], subject: str, body: str, cc: Optional[list[str]] = None,
                 from_name: Optional[str] = None, in_reply_to: Optional[str] = None,
                 references: Optional[str] = None, thread_id: Optional[str] = None,
                 message_id: Optional[str] = None, key: Optional[str] = None):
        self.message_id = message_id          # deterministic per send key (Gmail may replace it; see the module doc)
        self.key = key                        # the send key, as the X-Salescoach-Key header: what recovery matches
        self.to = list(to)
        self.cc = list(cc or [])
        self.subject = subject
        self.body = body
        self.from_name = from_name
        self.in_reply_to = in_reply_to
        self.references = references
        self.thread_id = thread_id


def _load_credentials(alias: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    client = json.loads((GMAIL_DIR / "credentials.json").read_text())
    client = client.get("installed") or client.get("web")
    registry = json.loads((GMAIL_DIR / "config.json").read_text())
    entry = (registry.get("accounts") or {}).get(alias) or {}
    token_path = Path(os.path.expanduser(str(entry.get("tokenFile") or f"tokens/{alias}.json")))
    if not token_path.is_absolute():            # gmail-multi stores it relative to ~/.gmail-mcp
        token_path = GMAIL_DIR / token_path
    token = json.loads(token_path.read_text())
    creds = Credentials(token=None, refresh_token=token["refresh_token"],
                        token_uri=client["token_uri"], client_id=client["client_id"],
                        client_secret=client["client_secret"], scopes=SCOPES)
    creds.refresh(Request())
    return creds, token.get("email") or entry.get("email")


class GmailProvider:
    def __init__(self, alias: str = "work", credentials=None, address: Optional[str] = None,
                 user_id: Optional[str] = None):
        self.alias = alias
        self.user_id = user_id                # cloud: whose grant this is; None for the machine-local alias
        self._creds = credentials
        self._service = None
        self.address = address

    @classmethod
    def for_user(cls, conn, user_id: str) -> "GmailProvider":
        """The acting user's own mailbox, from their oauth_tokens grant. Raises tokens.NoToken when
        Gmail was never connected (or its scopes are missing), tokens.NeedsReconsent when the grant
        is dead, tokens.TokenError when Google could not refresh it. Never touches a token file."""
        from google.oauth2.credentials import Credentials
        from . import tokens
        if not tokens.has_feature(conn, user_id, "gmail"):
            row = tokens.get(conn, user_id)
            if row is not None and row["status"] != "active":
                raise tokens.NeedsReconsent("the Google connection needs to be linked again from your profile page")
            raise tokens.NoToken("Gmail is not connected for this user: connect it from your profile page")
        access = tokens.access_token(conn, user_id)
        row = tokens.get(conn, user_id)
        return cls(alias=f"user:{user_id}", credentials=Credentials(token=access), address=row["email"], user_id=user_id)

    def _svc(self):
        if self._service is None:
            from googleapiclient.discovery import build
            if self._creds is None:
                self._creds, self.address = _load_credentials(self.alias)
            # The bundled (static) discovery document; its root is https://gmail.googleapis.com/ unless the
            # end-to-end harness overrides it (salescoach/endpoints.py, GOOGLE_API_BASE with SALESCOACH_E2E=1).
            from .. import endpoints
            root = endpoints.override(endpoints.GOOGLE_API_BASE)
            options = {"api_endpoint": root + "/"} if root else None
            self._service = build("gmail", "v1", credentials=self._creds, cache_discovery=False,
                                  client_options=options)
        return self._service

    def profile(self) -> dict:
        return self._svc().users().getProfile(userId="me").execute()

    def _raw(self, msg: OutgoingEmail) -> dict:
        self._svc()
        mime = EmailMessage()
        mime["To"] = ", ".join(msg.to)
        if msg.cc:
            mime["Cc"] = ", ".join(msg.cc)
        mime["From"] = formataddr((msg.from_name, self.address)) if msg.from_name else self.address
        mime["Subject"] = msg.subject
        mime["Message-ID"] = msg.message_id or make_msgid(domain=(self.address or "localhost").split("@")[-1])
        if msg.key:
            mime[KEY_HEADER] = msg.key
        if msg.in_reply_to:
            mime["In-Reply-To"] = msg.in_reply_to
            mime["References"] = msg.references or msg.in_reply_to
        mime.set_content(msg.body)
        body = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
        if msg.thread_id:
            body["threadId"] = msg.thread_id
        return body

    def find_sent(self, rfc822_message_id: str) -> Optional[dict]:
        """Read-only: the sent message carrying this Message-ID header, if Gmail has it."""
        found = self._svc().users().messages().list(
            userId="me", q=f"in:sent rfc822msgid:{rfc822_message_id.strip('<>')}", maxResults=1).execute()
        messages = found.get("messages") or []
        return {"message_id": messages[0]["id"], "thread_id": messages[0].get("threadId")} if messages else None

    def find_draft(self, rfc822_message_id: str) -> Optional[dict]:
        """Read-only: the Gmail draft carrying this Message-ID header, if Gmail has it."""
        found = self._svc().users().messages().list(
            userId="me", q=f"in:drafts rfc822msgid:{rfc822_message_id.strip('<>')}", maxResults=1).execute()
        messages = found.get("messages") or []
        return {"message_id": messages[0]["id"], "thread_id": messages[0].get("threadId")} if messages else None

    def _headers(self, message_id: str, names: list[str]) -> dict:
        """Read-only, metadata format: the named headers of one message, lower-cased names."""
        got = self._svc().users().messages().get(userId="me", id=message_id, format="metadata",
                                                 metadataHeaders=names).execute()
        headers = ((got.get("payload") or {}).get("headers") or [])
        # Unfolded (RFC 5322): a long header may come back folded from a less careful server.
        out = {h.get("name", "").lower(): " ".join(str(h.get("value", "")).split()) for h in headers}
        out["_thread_id"] = got.get("threadId")
        return out

    def _find_by_key(self, folder: str, key: str, to, subject: Optional[str], since: Optional[str]) -> Optional[dict]:
        """List `folder` since `since` and return the message whose X-Salescoach-Key is `key` (To and
        Subject are checked too, as a guard against a header that somehow moved)."""
        query = f"in:{folder}"
        if since:
            query += f" after:{_iso_ms(since) // 1000}"
        found = self._svc().users().messages().list(userId="me", q=query, maxResults=RECOVERY_LIMIT).execute()
        wanted_to = {a.strip().lower() for a in (to or []) if a}
        for item in found.get("messages") or []:
            headers = self._headers(item["id"], [KEY_HEADER, "To", "Subject", "Message-ID"])
            if headers.get(KEY_HEADER.lower()) != key:
                continue
            seen_to = {parseaddr(part)[1].lower() for part in (headers.get("to") or "").split(",") if part.strip()}
            if wanted_to and not wanted_to <= seen_to:
                continue
            if subject is not None and (headers.get("subject") or "").strip() != subject.strip():
                continue
            return {"message_id": item["id"], "thread_id": item.get("threadId") or headers.get("_thread_id"),
                    "rfc822_id": headers.get("message-id") or None}
        return None

    def find_sent_by_key(self, key: str, to=(), subject: Optional[str] = None, since: Optional[str] = None) -> Optional[dict]:
        """Read-only: the sent message carrying this send key, if Gmail has it."""
        return self._find_by_key("sent", key, to, subject, since)

    def find_draft_by_key(self, key: str, to=(), subject: Optional[str] = None, since: Optional[str] = None) -> Optional[dict]:
        found = self._find_by_key("drafts", key, to, subject, since)
        if found:
            drafts = self._svc().users().drafts().list(userId="me", q=f"rfc822msgid:{(found.get('rfc822_id') or '').strip('<>')}",
                                                       maxResults=1).execute() if found.get("rfc822_id") else {}
            items = drafts.get("drafts") or []
            found["draft_id"] = items[0].get("id") if items else None
        return found

    def send(self, msg: OutgoingEmail) -> dict:
        sent = self._svc().users().messages().send(userId="me", body=self._raw(msg)).execute()
        out = {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}
        try:                                       # the Message-ID Gmail actually stored, for threading later
            out["rfc822_id"] = self._headers(sent["id"], ["Message-ID"]).get("message-id") or None
        except Exception:
            out["rfc822_id"] = None
        return out

    def save_draft(self, msg: OutgoingEmail) -> dict:
        draft = self._svc().users().drafts().create(
            userId="me", body={"message": self._raw(msg)}).execute()
        message = draft.get("message") or {}
        return {"draft_id": draft.get("id"), "message_id": message.get("id"),
                "thread_id": message.get("threadId")}

    def read_replies(self, thread_id: str, since: str) -> list[dict]:
        """Messages in one thread that arrived after `since` (ISO timestamp), oldest first.

        Read-only (threads.get). Every sender is returned, the seller included;
        the caller decides whose messages count as replies. Bodies are
        untrusted text.
        """
        thread = self._svc().users().threads().get(userId="me", id=thread_id, format="full").execute()
        cutoff = _iso_ms(since)
        out = []
        for msg in thread.get("messages") or []:
            internal = int(msg.get("internalDate") or 0)
            if internal <= cutoff:
                continue
            payload = msg.get("payload") or {}
            headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers") or []}
            name, addr = parseaddr(headers.get("from", ""))
            out.append({
                "message_id": msg.get("id"), "thread_id": msg.get("threadId") or thread_id,
                "from_addr": (addr or "").lower(), "from_name": name or None,
                "to": headers.get("to", ""), "cc": headers.get("cc", ""), "subject": headers.get("subject"),
                "rfc822_id": headers.get("message-id"), "internal_ms": internal,
                "received_at": datetime.fromtimestamp(internal / 1000, tz=timezone.utc).isoformat(timespec="seconds"),
                "label_ids": list(msg.get("labelIds") or []), "body": message_text(payload),
            })
        return sorted(out, key=lambda m: m["internal_ms"])

    def thread_ids(self, query: str, limit: int = 10) -> list[str]:
        """Read-only search: ids of the threads matching a Gmail query."""
        found = self._svc().users().threads().list(userId="me", q=query, maxResults=limit).execute()
        return [t["id"] for t in found.get("threads") or []]


# ---- which mailbox ------------------------------------------------------------------

def provider_for(conn) -> GmailProvider:
    """The mailbox the acting user sends from: their own grant in cloud mode (raises tokens.NoToken /
    NeedsReconsent when it is missing or dead), the machine-local alias on a local install."""
    from .. import config, identity
    actor = identity.actor_of(conn)
    if identity.cloud() or (actor is not None and not actor.is_local):
        return GmailProvider.for_user(conn, actor.user_id)
    return GmailProvider((config.load("policy").get("email") or {}).get("account", "work"))


def call_factory(factory, conn) -> GmailProvider:
    """Call a gmail factory: the per-user one takes the connection; a test's stand-in takes nothing."""
    try:
        wants = bool(inspect.signature(factory).parameters)
    except (TypeError, ValueError):
        wants = False
    return factory(conn) if wants else factory()


# ---- read-only helpers for reply ingestion -----------------------------------------

def _iso_ms(since) -> int:
    if not since:
        return 0
    ts = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def message_text(payload: dict) -> str:
    """The readable body of a Gmail message payload: text/plain, else HTML as text. Attachments skipped."""
    plain, rich = [], []

    def walk(part):
        for sub in part.get("parts") or []:
            walk(sub)
        if part.get("filename"):
            return
        data = (part.get("body") or {}).get("data")
        mime = (part.get("mimeType") or "").lower()
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(_b64(data))

    walk(payload or {})
    if plain:
        return "\n".join(plain).replace("\r\n", "\n").strip()
    return html_to_text("\n".join(rich)) if rich else ""
