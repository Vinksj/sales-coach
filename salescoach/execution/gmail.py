"""Gmail execution: the only code path that puts mail on the wire.

Reuses the gmail-multi OAuth client (~/.gmail-mcp/credentials.json) and the
account's refresh token (~/.gmail-mcp/tokens/<alias>.json). The token file is
READ-ONLY here: the access token is refreshed in memory and never written
back, so this cannot race or corrupt the MCP server's copy.

No LLM touches this module. It sends exactly the bytes it is handed, and only
the policy/approval layer (execution/policy.py + workflow) may call send().

Outbound HTTP goes through google-api-python-client to two fixed Google hosts
rather than lib/safefetch: safefetch is GET-only and exists to stop a
response from choosing the next URL, which cannot happen against a fixed API.
"""
import base64
import html
import json
import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path
from typing import Optional

GMAIL_DIR = Path(os.path.expanduser("~/.gmail-mcp"))
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class OutgoingEmail:
    def __init__(self, to: list[str], subject: str, body: str, cc: Optional[list[str]] = None,
                 from_name: Optional[str] = None, in_reply_to: Optional[str] = None,
                 references: Optional[str] = None, thread_id: Optional[str] = None,
                 message_id: Optional[str] = None):
        self.message_id = message_id          # deterministic per send key, so Sent can be searched later
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
    def __init__(self, alias: str = "work"):
        self.alias = alias
        self._service = None
        self.address = None

    def _svc(self):
        if self._service is None:
            from googleapiclient.discovery import build
            creds, self.address = _load_credentials(self.alias)
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
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

    def send(self, msg: OutgoingEmail) -> dict:
        sent = self._svc().users().messages().send(userId="me", body=self._raw(msg)).execute()
        return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}

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
