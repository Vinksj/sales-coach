"""Per-rep Gmail (execution/gmail.py GmailProvider.for_user, provider_for; the policy guard).

Pinned: provider_for is the machine-local alias on a local install and the acting user's own grant
in cloud mode; for_user picks THAT user's token, refuses when Gmail was never connected, when only
Calendar was, and when the grant needs re-consent; approved_by carries user:<id>; every send
carries the X-Salescoach-Key header, the id/threadId from the response are stored at once and the
Message-ID Gmail kept (it may replace ours) is read back; an unknown-outcome send is recovered by
that header from Sent, never re-sent; in cloud a manager and a service-mode actor get SendRefused
while the owner at the keyboard sends; auto-send is off in cloud; the replies duty runs per user
on that user's grant and skips a user without one; Setup > Connections shows the org-level Google
status in cloud mode.
"""
import base64
import email as email_lib
import json
from email import policy as email_policy

import pytest

from salescoach import googleauth, identity, users
from salescoach.automation import autosend, scheduler
from salescoach.execution import gmail as gmail_mod
from salescoach.execution import policy, tokens
from salescoach.setupui import state
from test_core_review_fixes import OkGmail, _email, _setup
from test_tokens import K1, google, keys  # noqa: F401

GMAIL = list(googleauth.FEATURE_SCOPES["gmail"])
CAL = list(googleauth.FEATURE_SCOPES["calendar"])


class Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeApi:
    """googleapiclient's users().messages()/drafts() chain over an in-memory mailbox. Like Gmail is
    reported to, it REPLACES the Message-ID header on send, so recovery must go by the key header."""

    def __init__(self, timeout_on_send=False):
        self.messages_by_id, self.order, self.sends, self.drafts_by_id = {}, [], 0, {}
        self.timeout_on_send = timeout_on_send

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def _store(self, raw_body, folder):
        mime = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw_body["raw"]), policy=email_policy.default)
        n = len(self.order) + 1
        headers = {k: " ".join(str(v).split()) for k, v in mime.items() if k.lower() != "message-id"}   # as Gmail returns them
        headers["Message-ID"] = f"<gmail-{n}@mail.gmail.com>"                  # Gmail's own, not ours
        rec = {"id": f"m-{n}", "threadId": raw_body.get("threadId") or f"t-{n}", "headers": headers, "folder": folder}
        self.messages_by_id[rec["id"]] = rec
        self.order.append(rec["id"])
        return rec

    def send(self, userId, body):
        rec = self._store(body, "sent")
        self.sends += 1
        if self.timeout_on_send:
            return Exec(TimeoutError("read timed out"))
        return Exec({"id": rec["id"], "threadId": rec["threadId"]})

    def create(self, userId, body):
        rec = self._store(body["message"], "drafts")
        self.drafts_by_id[f"d-{rec['id']}"] = rec["id"]
        return Exec({"id": f"d-{rec['id']}", "message": {"id": rec["id"], "threadId": rec["threadId"]}})

    def get(self, userId, id, format=None, metadataHeaders=()):
        rec = self.messages_by_id[id]
        wanted = {h.lower() for h in metadataHeaders}
        return Exec({"id": id, "threadId": rec["threadId"],
                     "payload": {"headers": [{"name": k, "value": v} for k, v in rec["headers"].items()
                                             if k.lower() in wanted]}})

    def list(self, userId, q="", maxResults=50):
        folder = "sent" if "in:sent" in q else "drafts" if "in:drafts" in q else None
        if "rfc822msgid:" in q:
            wanted = q.split("rfc822msgid:")[1].split()[0]
            ids = [d for d, m in self.drafts_by_id.items() if self.messages_by_id[m]["headers"]["Message-ID"].strip("<>") == wanted]
            return Exec({"drafts": [{"id": d} for d in ids]})
        items = [{"id": i, "threadId": self.messages_by_id[i]["threadId"]} for i in reversed(self.order)
                 if self.messages_by_id[i]["folder"] == folder]
        return Exec({"messages": items[:maxResults]})


def provider_on(api, user_id="local"):
    p = gmail_mod.GmailProvider(alias="test", credentials=object(), address="maya@tessel.test", user_id=user_id)
    p._service = api
    return p


def grant(conn, user_id, scopes=GMAIL, email="maya@tessel.test", access="at-cached"):
    tokens.store(conn, user_id, f"1//rt-{user_id}", scopes, email, access_token=access, expires_in=3600)
    conn.commit()


# ---- which mailbox --------------------------------------------------------------------------------

def test_provider_for_local_is_the_machine_alias(db, monkeypatch):
    monkeypatch.delenv(identity.MODE_ENV, raising=False)
    p = gmail_mod.provider_for(db)
    assert isinstance(p, gmail_mod.GmailProvider) and p.alias == "work" and p.user_id is None and p._creds is None


def test_for_user_picks_that_users_grant_and_refuses_missing_or_dead(db, keys, dialect):
    with pytest.raises(tokens.NoToken, match="not connected"):
        gmail_mod.GmailProvider.for_user(db, "local")
    grant(db, "local", scopes=CAL)
    with pytest.raises(tokens.NoToken, match="Gmail is not connected"):                   # Calendar alone is not Gmail
        gmail_mod.GmailProvider.for_user(db, "local")
    grant(db, "local", scopes=GMAIL, access="at-maya")
    p = gmail_mod.GmailProvider.for_user(db, "local")
    assert p.user_id == "local" and p.address == "maya@tessel.test" and p._creds.token == "at-maya"
    assert p._creds.refresh_token is None                                                  # never handed a refresh token
    tokens.mark(db, "local", "needs_reconsent", "invalid_grant")
    db.commit()
    with pytest.raises(tokens.NeedsReconsent, match="linked again"):
        gmail_mod.GmailProvider.for_user(db, "local")
    tokens.delete(db, "local")
    db.commit()
    with pytest.raises(tokens.NoToken):
        gmail_mod.GmailProvider.for_user(db, "local")
    if dialect == "postgres":
        asha = users.create(db, "asha@tessel.test", "Asha")
        bala = users.create(db, "bala@tessel.test", "Bala")
        grant(db, asha["id"], email="asha@tessel.test", access="at-asha")
        grant(db, bala["id"], email="bala@tessel.test", access="at-bala")
        a, b = gmail_mod.GmailProvider.for_user(db, asha["id"]), gmail_mod.GmailProvider.for_user(db, bala["id"])
        assert (a.address, a._creds.token, b.address, b._creds.token) == ("asha@tessel.test", "at-asha", "bala@tessel.test", "at-bala")
        with identity.as_user(db, asha["id"]):
            assert gmail_mod.provider_for(db).address == "asha@tessel.test"              # the acting user's own


def test_call_factory_accepts_both_shapes(db):
    fake = OkGmail()
    assert gmail_mod.call_factory(lambda: fake, db) is fake
    assert gmail_mod.call_factory(lambda conn: (conn, fake), db) == (db, fake)


# ---- sending ---------------------------------------------------------------------------------------

def test_send_carries_the_key_header_stores_ids_and_reads_the_message_id_back(db):
    deal, call = _setup(db)
    eid = _email(db, deal, call)
    api = FakeApi()
    result = policy.approve_and_send(db, eid, provider_on(api))
    row = db.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    assert result["status"] == "sent" and row["status"] == "sent" and row["approved_by"] == "user:local"
    assert (row["gmail_message_id"], row["gmail_thread_id"]) == ("m-1", "t-1")
    stored = api.messages_by_id["m-1"]["headers"]
    assert stored[gmail_mod.KEY_HEADER] == row["idempotency_key"] and len(row["idempotency_key"]) == 64
    assert stored["Message-ID"] == "<gmail-1@mail.gmail.com>" and row["rfc822_message_id"] == "<gmail-1@mail.gmail.com>"
    assert stored["To"] == "arjun@northwind.test" and "From" in stored


def test_unknown_outcome_is_recovered_by_the_key_header_not_resent(db):
    deal, call = _setup(db)
    eid = _email(db, deal, call)
    api = FakeApi(timeout_on_send=True)
    with pytest.raises(TimeoutError):
        policy.approve_and_send(db, eid, provider_on(api))
    row = db.execute("SELECT status, error, idempotency_key FROM emails WHERE id=?", (eid,)).fetchone()
    assert row["status"] == "sending" and "delivery unknown" in row["error"] and api.sends == 1
    # a decoy in Sent with another key and the same recipient must not be mistaken for ours
    api.timeout_on_send = False
    other = provider_on(api)
    other.send(gmail_mod.OutgoingEmail(to=["arjun@northwind.test"], subject="Re: pilot", body="x", key="k" * 64))
    result = policy.approve_and_send(db, eid, provider_on(api))
    assert result["recovered"] and result["status"] == "sent" and api.sends == 2                # nothing sent again
    row = db.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    assert (row["gmail_message_id"], row["rfc822_message_id"]) == ("m-1", "<gmail-1@mail.gmail.com>")
    assert api.messages_by_id["m-1"]["headers"][gmail_mod.KEY_HEADER] == row["idempotency_key"]
    # and when Sent holds nothing with our key, the retry stays refused until the person confirms
    eid2 = _email(db, deal, call)
    with pytest.raises(TimeoutError):
        policy.approve_and_send(db, eid2, provider_on(FakeApi(timeout_on_send=True)))
    with pytest.raises(policy.SendRefused, match="Check Gmail Sent"):
        policy.approve_and_send(db, eid2, provider_on(FakeApi()))


def test_find_by_key_matches_recipient_and_subject_too(db):
    api = FakeApi()
    p = provider_on(api)
    p.send(gmail_mod.OutgoingEmail(to=["a@x.test"], subject="Hello", body="b", key="key-1"))
    assert p.find_sent_by_key("key-1", to=["a@x.test"], subject="Hello")["message_id"] == "m-1"
    assert p.find_sent_by_key("key-1", to=["b@x.test"], subject="Hello") is None
    assert p.find_sent_by_key("key-1", to=["a@x.test"], subject="Other") is None
    assert p.find_sent_by_key("key-2") is None
    p.save_draft(gmail_mod.OutgoingEmail(to=["a@x.test"], subject="Draft", body="b", key="key-d"))
    found = p.find_draft_by_key("key-d", to=["a@x.test"], subject="Draft")
    assert found["message_id"] == "m-2" and found["draft_id"] == "d-m-2"
    assert p.find_sent_by_key("key-d") is None                                              # drafts are not Sent


def test_in_cloud_only_the_owner_at_the_keyboard_may_send(db, monkeypatch):
    deal, call = _setup(db)
    eid = _email(db, deal, call)
    monkeypatch.setenv(identity.MODE_ENV, "cloud")                                          # the store is open already
    manager = identity.Actor("u-mani", identity.INTERACTIVE, "manager", {"name": "Mani", "emails": ["m@tessel.test"]})
    with identity.as_actor(db, manager):
        with pytest.raises(policy.SendRefused, match="belongs to another user; only its owner can act on it"):
            policy.approve_and_send(db, eid, OkGmail())
    with identity.as_actor(db, identity.LOCAL_ACTOR.as_service()):
        with pytest.raises(policy.SendRefused, match="owner at the keyboard"):
            policy.approve_and_send(db, eid, OkGmail())
    assert db.execute("SELECT status FROM emails WHERE id=?", (eid,)).fetchone()[0] == "drafted"   # untouched
    owner = OkGmail()
    with identity.as_actor(db, identity.LOCAL_ACTOR):
        assert policy.approve_and_send(db, eid, owner)["status"] == "sent"
    assert len(owner.sent) == 1 and owner.sent[0].key


def test_auto_send_is_off_in_cloud(db, monkeypatch):
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    touched = []
    assert autosend.run_once(db, lambda: touched.append(1)) == {"skipped": "auto-send is off in a cloud install"}
    assert touched == []
    duty = next(d for d in scheduler.default_duties() if d.name == "autosend")
    with identity.as_actor(db, identity.LOCAL_ACTOR.as_service()):                          # as the scheduler runs it
        assert duty.run(db)["skipped"].startswith("auto-send is off")


def test_replies_duty_runs_on_the_users_own_grant_in_cloud(db, keys, monkeypatch):
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    duty = scheduler._replies_duty()
    with identity.as_actor(db, identity.LOCAL_ACTOR.as_service()):                          # as the scheduler runs it
        assert duty.run(db)["skipped"].startswith("Gmail not connected")                    # no grant: skipped, no backoff
        grant(db, "local")
        seen = []

        class Reader:
            def read_replies(self, thread_id, since):
                seen.append(thread_id)
                return []

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(scheduler, "gmail_for", lambda conn: Reader())
            assert duty.run(db) == {"threads": 0, "new": [], "errors": []}
        tokens.mark(db, "local", "needs_reconsent", "invalid_grant")
        db.commit()
        assert "linked again" in duty.run(db)["skipped"]


def test_setup_connections_in_cloud_show_the_org_level_google_status(db, monkeypatch):
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    for name in (googleauth.CLIENT_ID_ENV, googleauth.CLIENT_SECRET_ENV, googleauth.DOMAINS_ENV, tokens.KEYS_ENV):
        monkeypatch.delenv(name, raising=False)
    rows = state.connections(db)
    assert [r["key"] for r in rows] == ["gmail", "calendar", "capture", "speech", "cli"]
    g = rows[0]
    assert not g["ok"] and g["status"] == "Not configured" and "GOOGLE_CLIENT_ID" in g["detail"] and "deploy-cloud" in g["fix"]
    assert "sign-in stored on the machine" not in g["detail"]
    monkeypatch.setenv(googleauth.CLIENT_ID_ENV, "c")
    monkeypatch.setenv(googleauth.CLIENT_SECRET_ENV, "s")
    monkeypatch.setenv(googleauth.DOMAINS_ENV, "tessel.test")
    monkeypatch.setenv(tokens.KEYS_ENV, K1)
    rows = state.connections(db)
    assert rows[0]["ok"] and rows[1]["ok"] and "tessel.test" in rows[0]["detail"] and rows[0]["fix"] == ""
    assert "gmail.compose and gmail.readonly" in rows[0]["enables"] and "calendar.readonly" in rows[1]["enables"]


def test_scopes_requested_are_exactly_the_minimum(db):
    assert googleauth.SIGNIN_SCOPES == ("openid", "email", "profile")
    assert GMAIL == ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"]
    assert CAL == ["https://www.googleapis.com/auth/calendar.readonly"]
    every = json.dumps(googleauth.FEATURE_SCOPES)
    assert "gmail.send" not in every and "gmail.modify" not in every and "mail.google.com" not in every
