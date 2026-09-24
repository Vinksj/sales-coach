"""Email policy and the one approved path to Gmail.

Default ALL_EMAILS_REQUIRE_APPROVAL. The two auto-send policies exist in
config so the architecture is ready; their executor lives in the execution
plugin, ships disabled, and still comes through approve_and_send().

Duplicate-send guard (tightened after the 2026-09-12 review):
  * approve_and_send() takes an IMMEDIATE transaction, refuses anything
    already sent/saved, and marks the row 'sending' before calling Gmail;
  * the Message-ID header is derived from the send key, so the same email
    always carries the same Message-ID and Gmail's Sent folder can be searched
    for it;
  * an error raised BEFORE Gmail could have accepted the message (bad
    credentials, a 4xx) marks the email failed, which may be retried;
  * any other error (timeout, connection reset, 5xx) leaves it 'sending' with
    "delivery unknown". A retry first searches Sent for the Message-ID; if the
    copy is there the email is marked sent, if not the retry is refused until
    the seller confirms "it was not sent" (acknowledge_not_sent).
A second copy to a customer is worse than a manual check.
"""
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import config, seller
from ..orchestrator import bus
from ..schemas.events import Event
from ..store.stores import now
from ..store import db
from ..validators import recipients as recipients_v
from ..validators import voice_lint

AUTO_POLICIES = {"LOW_RISK_FOLLOWUPS_AUTO_SEND", "APPROVED_CONTACTS_AUTO_SEND"}
KNOWN = AUTO_POLICIES | {"ALL_EMAILS_REQUIRE_APPROVAL", "NEVER_AUTO_SEND"}
UNKNOWN_DELIVERY = "delivery unknown"


@dataclass
class Decision:
    action: str          # require_approval | block
    reason: str
    issues: list


def current_policy() -> str:
    policy = config.load("policy").get("email_policy", "ALL_EMAILS_REQUIRE_APPROVAL")
    return policy if policy in KNOWN else "ALL_EMAILS_REQUIRE_APPROVAL"


def _stored_user_added(email_row) -> list:
    try:
        return json.loads(email_row["user_added_addrs"] or "[]")
    except (IndexError, KeyError, TypeError, ValueError):
        return []


def evaluate(conn, email_row, user_added=()) -> Decision:
    to = json.loads(email_row["to_addrs"] or "[]")
    cc = json.loads(email_row["cc_addrs"] or "[]")
    allowed = recipients_v.allowed_recipients(conn, email_row["call_id"], email_row["deal_id"])
    added = list(user_added) + _stored_user_added(email_row)
    issues = [{"kind": "recipient", "detail": v, "severity": "block"}
              for v in recipients_v.check(to, cc, allowed, added)]
    issues += [i.as_dict() for i in voice_lint.lint(email_row["subject"] or "", email_row["body"] or "")]
    if any(i["severity"] == "block" for i in issues):
        return Decision("block", "fix the blocking issues before sending", issues)
    policy = current_policy()
    if policy in AUTO_POLICIES:
        return Decision("require_approval", f"{policy} is configured, but auto-send only runs through the "
                                            "execution plugin's executor, so this still needs your Send", issues)
    return Decision("require_approval", f"{policy}: waiting for your Send", issues)


class SendRefused(RuntimeError):
    pass


def _definitely_not_sent(exc) -> bool:
    """True only for errors raised before Gmail could have accepted the message."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is not None:
        try:
            return int(status) < 500
        except (TypeError, ValueError):
            return False
    # Errors raised while building the message or loading credentials happen before any request:
    # a bad header (ValueError from EmailMessage), a corrupt token file (JSONDecodeError is a ValueError).
    if isinstance(exc, (ValueError, TypeError)):
        return True
    return type(exc).__name__ in {"RefreshError", "DefaultCredentialsError", "FileNotFoundError",
                                  "PermissionError", "KeyError"}


def _from_name():
    """policy.yaml email.from_name when set (an override), else the seller's name from the profile."""
    return (config.load("policy").get("email") or {}).get("from_name") or seller.from_name()


def _rfc822_id(key: str, gmail) -> str:
    # The sending mailbox decides; without one, the seller's first address; never a made-up company.
    domain = (getattr(gmail, "address", None) or seller.primary_email() or "salescoach.invalid").split("@")[-1]
    return f"<sc-{key[:32]}@{domain}>"


def _write(conn, sql, params, attempts=5):
    """Post-send bookkeeping must land even if another writer holds the lock for a moment."""
    for i in range(attempts):
        try:
            conn.execute(sql, params)
            return
        except db.OperationalError as exc:
            # Only SQLite has a "database is locked" to wait out; Postgres queues the writer itself.
            if conn.dialect != db.SQLITE or "locked" not in str(exc) or i == attempts - 1:
                raise
            time.sleep(1 + i)


def _mark_sent(conn, row, mode, result):
    status = "sent" if mode == "send" else "saved_to_gmail"
    _write(conn, "UPDATE emails SET status=?, gmail_message_id=?, gmail_thread_id=?, gmail_draft_id=?, "
                 "sent_at=?, error=NULL, updated_at=? WHERE id=?",
           (status, result.get("message_id"), result.get("thread_id"), result.get("draft_id"),
            now() if mode == "send" else None, now(), row["id"]))
    if (row["draft_body"] or "") != (row["body"] or ""):
        _write(conn, "INSERT INTO email_edits(email_id,draft_body,final_body,created_at) VALUES (?,?,?,?)",
               (row["id"], row["draft_body"], row["body"], now()))
    bus.publish(conn, Event(type="EMAIL_SENT" if mode == "send" else "EMAIL_APPROVED",
                            entity_id=row["call_id"] or row["deal_id"],     # nudges have no call
                            dedupe_key=f"EMAIL:{mode}:{row['id']}:{row['version']}",
                            payload={"email_id": row["id"], "mode": mode, "kind": row["kind"]}))
    conn.commit()
    return status


def mark_sent_manually(conn, email_id: int, by: str = "user:ui") -> bool:
    """The seller sent a Gmail draft by hand: record the send so everything downstream
    (follow-up counts, call state, Jarvis) sees it exactly as if we had sent it."""
    row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if row is None or row["status"] != "saved_to_gmail":
        return False
    _write(conn, "UPDATE emails SET status='sent', sent_at=?, approved_by=COALESCE(approved_by, ?), updated_at=? "
                 "WHERE id=?", (now(), by, now(), email_id))
    bus.publish(conn, Event(type="EMAIL_SENT", entity_id=row["call_id"] or row["deal_id"],
                            dedupe_key=f"EMAIL:send:{row['id']}:{row['version']}",
                            payload={"email_id": row["id"], "mode": "send", "kind": row["kind"],
                                     "sent_from": "gmail_by_hand"}))
    conn.commit()
    return True


def _recover(conn, row, gmail):
    """An earlier attempt's outcome is unknown: look for its copy where that attempt would have put it
    (Sent for a send, Drafts for a save to Gmail Drafts)."""
    attempted = row["attempt_mode"] or "send"
    finder = getattr(gmail, "find_sent" if attempted == "send" else "find_draft", None)
    if not finder or not row["rfc822_message_id"]:
        return None
    try:
        found = finder(row["rfc822_message_id"])
    except Exception:
        return None
    if not found:
        return None
    return {"status": _mark_sent(conn, row, attempted, found), "duplicate": False, "recovered": True, **found}


def approve_and_send(conn, email_id: int, gmail, mode: str = "send", approved_by: str = "user:ui",
                     user_added=()) -> dict:
    """Send (mode='send') or save to Gmail Drafts (mode='draft') one approved email, exactly once."""
    if mode not in ("send", "draft"):
        raise ValueError(mode)
    if conn.in_transaction:
        conn.commit()
    try:
        # The row is ours until COMMIT: BEGIN IMMEDIATE on SQLite, SELECT ... FOR UPDATE on Postgres.
        row = conn.lock_rows("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
        if row is None:
            raise SendRefused("no such email")
        if row["status"] in ("sent", "saved_to_gmail"):
            conn.execute("ROLLBACK")
            return {"status": row["status"], "duplicate": True, "message_id": row["gmail_message_id"]}
        if row["status"] == "sending":
            conn.execute("ROLLBACK")
            recovered = _recover(conn, row, gmail)
            if recovered:
                return recovered
            if (row["attempt_mode"] or "send") == "draft":
                # The stuck attempt only tried to save a draft. Nothing can have been sent, and a second
                # copy in Gmail Drafts is harmless, so the row is released and this attempt proceeds.
                _write(conn, "UPDATE emails SET status='failed', error=?, updated_at=? WHERE id=?",
                       ("an earlier save to Gmail Drafts did not finish; retried", now(), email_id))
                conn.commit()
                return approve_and_send(conn, email_id, gmail, mode=mode, approved_by=approved_by,
                                        user_added=user_added)
            raise SendRefused("an earlier send may already have gone out and Gmail's Sent folder does not "
                              "confirm it. Check Gmail Sent; if it is not there, press \"It was not sent\" "
                              "to allow a retry.")
        if row["status"] == "rejected":
            raise SendRefused("this draft was rejected")
        if re.search(r"[\r\n]", row["subject"] or ""):
            raise SendRefused("the subject contains a line break")
        decision = evaluate(conn, row, user_added)
        if decision.action == "block":
            raise SendRefused("; ".join(i["detail"] for i in decision.issues if i["severity"] == "block"))
        to = json.loads(row["to_addrs"])
        key = hashlib.sha256(f"{email_id}|{row['version']}|{mode}|{row['subject']}|{row['body']}|{to}".encode()
                             ).hexdigest()
        rfc822 = _rfc822_id(key, gmail)
        conn.execute("UPDATE emails SET status='sending', attempt_mode=?, approved_at=?, approved_by=?, "
                     "idempotency_key=?, rfc822_message_id=?, policy_decision=?, lint=?, error=NULL, updated_at=? "
                     "WHERE id=?",
                     (mode, now(), approved_by, key, rfc822, decision.reason, json.dumps(decision.issues), now(),
                      email_id))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    from .gmail import OutgoingEmail
    msg = OutgoingEmail(to=to, cc=json.loads(row["cc_addrs"] or "[]"), subject=row["subject"], body=row["body"],
                        from_name=_from_name(), message_id=rfc822)
    try:
        result = gmail.send(msg) if mode == "send" else gmail.save_draft(msg)
    except Exception as exc:
        if _definitely_not_sent(exc):
            _write(conn, "UPDATE emails SET status='failed', error=?, updated_at=? WHERE id=?",
                   (f"not sent: {type(exc).__name__}: {exc}"[:1000], now(), email_id))
        else:
            _write(conn, "UPDATE emails SET error=?, updated_at=? WHERE id=?",
                   (f"{UNKNOWN_DELIVERY}: {type(exc).__name__}: {exc}. Check Gmail Sent before doing anything."
                    [:1000], now(), email_id))
        conn.commit()
        raise
    status = _mark_sent(conn, row, mode, result)
    return {"status": status, "duplicate": False, **result}


def acknowledge_not_sent(conn, email_id: int, by: str = "user:ui") -> bool:
    """The seller checked Gmail Sent and the stuck email is not there: allow a retry."""
    row = conn.execute("SELECT status FROM emails WHERE id=?", (email_id,)).fetchone()
    if row is None or row["status"] != "sending":
        return False
    conn.execute("UPDATE emails SET status='failed', error=?, updated_at=? WHERE id=?",
                 (f"confirmed not sent by {by}; safe to retry", now(), email_id))
    conn.commit()
    return True


def call_age_hours(call_row) -> float:
    ended = call_row["ended_at"] or call_row["started_at"]
    if not ended:
        return 0.0
    ts = datetime.fromisoformat(ended)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
