"""Per-rep recorder connections (sources/connections.py): storage, polling, ownership.

Both backends: a key is AES-GCM ciphertext bound to its row, never the plaintext and never in public();
a poll imports the account's new meetings as the connection's owner (source_ref <kind>:<owner>:<id>,
history=False, the owner's own turns on 'me', CALL_ENDED published), is not due again until its
interval, skips what it already has; a 401 stops the connection with a reconnect message; a 429 is
remembered and waited out; errors back off exponentially; a transcript not ready yet is retried; one
connection failing does not stop the next; a colleague-only meeting is not imported; Disconnect deletes
the secrets; the admin's allow-list switches a kind off; `tokens rotate` re-encrypts recorder keys; an
upload in cloud mode keys on the acting rep.

Postgres (two real users under the row-level policies): the SAME meeting in two reps' accounts becomes
two independent calls, one per rep, each seen from its owner's side; a rep with no connection gets
nothing; B cannot read A's connection or meetings; one user's failing connection does not stop the
per-user duty from polling the next user.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from salescoach import identity, users
from salescoach.execution import tokens
from salescoach.sources import base, connections, recorders
from salescoach.sources.recorders import Account

import recorder_fakes as rf
from test_tokens import K1, K2, keys  # noqa: F401

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
MAYA = [("Maya Iyer", "I will send the pricing sheet by Friday."), ("Chen Wu", "We lose two days on every dispute.")]


@pytest.fixture
def fake(monkeypatch):
    fake = rf.FakeRecorders()
    monkeypatch.setattr(connections, "TRANSPORT", fake.transport())
    return fake


def ff(tid, speakers=MAYA, **kw):
    return rf.fireflies_transcript(tid, speakers, **kw)


def connect(db, kind, key, account=None):
    return connections.save_key(db, kind, key, account)


def row_text(db, connection_id) -> str:
    return json.dumps(dict(db.execute("SELECT * FROM source_connections WHERE id=?", (connection_id,)).fetchone()),
                      default=str)


# ---- storage --------------------------------------------------------------------------------------

def test_the_key_is_ciphertext_bound_to_its_row_and_never_public(db, keys):
    c = connect(db, "fireflies", "ff-live-KEY-123", Account(email="maya@tessel.test", name="Maya Iyer"))
    assert c["owner_id"] == "local" and c["status"] == "active" and c["account_email"] == "maya@tessel.test"
    assert c["has_key"] and not any(k in c for k in connections.SECRET_COLUMNS)
    assert "ff-live-KEY-123" not in row_text(db, c["id"])                         # nowhere in the row
    raw = connections.get(db, c["id"])
    assert raw["key_id"] == "k1" and raw["secret_enc"] and connections.api_key_of(raw) == "ff-live-KEY-123"
    # moved to another kind's row, the ciphertext does not open (associated data = owner, kind, column)
    other = connect(db, "fathom", "fathom-key")
    db.execute("UPDATE source_connections SET secret_enc=? WHERE id=?", (raw["secret_enc"], other["id"]))
    db.commit()
    with pytest.raises(tokens.TokenError):
        connections.api_key_of(connections.get(db, other["id"]))
    # reconnecting replaces the key in place: still one row per (owner, kind)
    again = connect(db, "fireflies", "ff-new-KEY-456")
    assert again["id"] == c["id"] and connections.api_key_of(connections.get(db, c["id"])) == "ff-new-KEY-456"
    assert db.execute("SELECT COUNT(*) FROM source_connections WHERE kind='fireflies'").fetchone()[0] == 1


def test_a_key_that_is_not_a_key_and_a_kind_that_is_not_allowed_are_refused(db, keys):
    for bad in ("", "has space", 'quo"te', "x" * 600):
        with pytest.raises(connections.ConnectionError_):
            connect(db, "fireflies", bad)
    with pytest.raises(connections.ConnectionError_):
        connect(db, "gong", "k")
    connections.set_allowed_kinds(["fathom"])
    assert connections.allowed_kinds() == ["fathom"] and not connections.allowed_is_default()
    with pytest.raises(connections.ConnectionError_, match="not allowed"):
        connect(db, "fireflies", "k")
    with pytest.raises(connections.ConnectionError_):
        connections.set_allowed_kinds(["zoom"])
    assert [d["kind"] for d in connections.catalog() if d["allowed"]] == ["fathom"]


def test_test_key_uses_the_typed_key_else_the_stored_one(db, keys, fake):
    fake.account("fireflies", "good", email="maya@tessel.test", name="Maya Iyer")
    assert connections.test_key(db, "fireflies", "good") == Account(email="maya@tessel.test", name="Maya Iyer")
    with pytest.raises(connections.ConnectionError_, match="Paste"):
        connections.test_key(db, "fireflies")
    connect(db, "fireflies", "good")
    assert connections.test_key(db, "fireflies").email == "maya@tessel.test"
    from salescoach.sources.adapters import SourceAuthError
    with pytest.raises(SourceAuthError):
        connections.test_key(db, "fireflies", "bad")


def test_disconnect_deletes_every_secret_and_stops_polling(db, keys, fake):
    fake.account("fireflies", "good", meetings=[ff("ff-1")])
    c = connect(db, "fireflies", "good")
    token = connections.new_webhook_token(db, c["id"])
    assert token and token not in row_text(db, c["id"])                           # only its hash is kept
    assert connections.poll_user(db, NOW)["fireflies"]["imported"] == 1
    assert connections.disconnect(db, c["id"])
    row = connections.get(db, c["id"])
    assert row["status"] == "disconnected" and all(row[k] is None for k in connections.SECRET_COLUMNS)
    assert row["state"]["recent"][0]["status"] == "imported"                       # My meetings still has it
    assert connections.poll_user(db, NOW + timedelta(days=1), force=True) == {}
    assert connections.new_webhook_token(db, c["id"]) is None
    assert not connections.disconnect(db, "rc-" + "0" * 32)


def test_rotate_re_encrypts_recorder_keys_under_the_newest_key(db, keys, monkeypatch):
    c = connect(db, "fireflies", "rotate-me-KEY")
    connections.set_signing_secret(db, c["id"], "whsec_c2lnbmluZy1zZWNyZXQ=")
    monkeypatch.setenv(tokens.KEYS_ENV, f"{K2},{K1}")
    report = connections.rotate(db)
    assert report == {"rotated": 1, "skipped": 0, "unreadable": [], "key": "k2"}
    row = connections.get(db, c["id"])
    assert row["key_id"] == "k2" and connections.api_key_of(row) == "rotate-me-KEY"
    assert connections._signing_secret(row) == "whsec_c2lnbmluZy1zZWNyZXQ="
    monkeypatch.setenv(tokens.KEYS_ENV, K2)                                       # the old key dropped: still readable
    assert connections.api_key_of(connections.get(db, c["id"])) == "rotate-me-KEY"


# ---- polling --------------------------------------------------------------------------------------

def test_a_poll_imports_new_meetings_as_the_owner_from_their_side(db, keys, fake):
    fake.account("fireflies", "good", email="maya@tessel.test", name="Maya Iyer", meetings=[ff("ff-1"), ff("ff-2")])
    c = connect(db, "fireflies", "good", Account(email="maya@tessel.test", name="Maya Iyer"))
    result = connections.poll_user(db, NOW)
    assert result == {"fireflies": {"listed": 2, "imported": 2, "needs_speaker": 0, "skipped": 0, "pending": 0,
                                    "errors": 0}}
    calls = db.execute("SELECT node_id, source, source_ref, history, wf_state, owner_id FROM calls ORDER BY source_ref").fetchall()
    assert [(r["source"], r["source_ref"], r["history"], r["wf_state"], r["owner_id"]) for r in calls] == [
        ("fireflies", "fireflies:local:ff-1", 0, "diarized", "local"), ("fireflies", "fireflies:local:ff-2", 0, "diarized", "local")]
    turns = db.execute("SELECT channel, text FROM turns WHERE call_id=? ORDER BY idx", (calls[0]["node_id"],)).fetchall()
    assert [(t["channel"], t["text"][:10]) for t in turns] == [("me", "I will sen"), ("them", "We lose tw")]
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='CALL_ENDED'").fetchone()[0] == 2
    me = db.execute("SELECT node_id FROM people WHERE user_id='local'").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM call_participants WHERE person_id=?", (me,)).fetchone()[0] == 2
    row = connections.get(db, c["id"])
    assert row["last_ok_at"] == NOW.isoformat() and row["failures"] == 0 and row["last_error"] is None
    assert row["next_poll_at"] == (NOW + timedelta(minutes=60)).isoformat()      # Fireflies: hourly
    assert {e["ext_id"]: e["status"] for e in row["state"]["recent"]} == {"ff-1": "imported", "ff-2": "imported"}
    assert connections.poll_user(db, NOW + timedelta(minutes=30)) == {}           # not due yet
    again = connections.poll_user(db, NOW + timedelta(minutes=61))
    assert again["fireflies"]["imported"] == 0 and db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2
    since = [r for r in fake.requests if r[2] == "POST"]
    assert since                                                                  # the listing ran with the rep's key only
    assert {r[1] for r in fake.requests} == {"good"}


def test_401_stops_the_connection_with_a_reconnect_message(db, keys, fake):
    fake.account("fireflies", "good", meetings=[ff("ff-1")])
    c = connect(db, "fireflies", "good")
    fake.fail[("fireflies", "good")] = (401, {})
    result = connections.poll_user(db, NOW)
    assert result["fireflies"]["status"] == "error" and "Reconnect" in result["fireflies"]["error"]
    row = connections.get(db, c["id"])
    assert row["status"] == "error" and "Reconnect" in row["last_error"] and row["next_poll_at"] is None
    del fake.fail[("fireflies", "good")]
    assert connections.poll_user(db, NOW + timedelta(days=1)) == {}               # stopped until the rep reconnects
    assert connections.poll_user(db, NOW + timedelta(days=1), force=True) == {}
    connect(db, "fireflies", "good")
    assert connections.poll_user(db, NOW + timedelta(days=1))["fireflies"]["imported"] == 1


def test_429_is_remembered_and_waited_out_and_errors_back_off(db, keys, fake):
    fake.account("fireflies", "good", meetings=[ff("ff-1")])
    c = connect(db, "fireflies", "good")
    fake.fail[("fireflies", "good")] = (429, {"retry-after": "7200"})
    assert "rate limiting" in connections.poll_user(db, NOW)["fireflies"]["error"]
    row = connections.get(db, c["id"])
    assert row["status"] == "active" and row["state"]["rate_limited_until"] == (NOW + timedelta(hours=2)).isoformat()
    n = len(fake.requests)
    del fake.fail[("fireflies", "good")]
    assert connections.poll_user(db, NOW + timedelta(minutes=90)) == {}           # remembered: no request at all
    assert len(fake.requests) == n
    assert connections.poll_user(db, NOW + timedelta(hours=2, minutes=1))["fireflies"]["imported"] == 1
    assert "rate_limited_until" not in connections.get(db, c["id"])["state"]
    # a plain error: exponential backoff, never faster than the interval, never slower than MAX_BACKOFF
    fake.fail[("fireflies", "good")] = (500, {})
    t = NOW + timedelta(days=1)
    waits = []
    for _ in range(4):
        connections.poll_user(db, t, force=True)
        row = connections.get(db, c["id"])
        waits.append(datetime.fromisoformat(row["next_poll_at"]) - t)
    assert waits == [timedelta(hours=2), timedelta(hours=4), timedelta(hours=6), timedelta(hours=6)]
    assert row["failures"] == 4 and "HTTP 500" in row["last_error"]


def test_one_connection_failing_does_not_stop_the_next(db, keys, fake):
    fake.account("fathom", "fk", meetings=[rf.fathom_meeting("rec-1", "maya@tessel.test", "Maya Iyer")])
    fake.account("fireflies", "good", meetings=[ff("ff-1")])
    connect(db, "fathom", "fk")
    connect(db, "fireflies", "good")
    fake.fail[("fathom", "fk")] = (503, {})
    result = connections.poll_user(db, NOW)
    assert "HTTP 503" in result["fathom"]["error"] and result["fireflies"]["imported"] == 1
    # a crash inside one adapter is contained the same way
    from salescoach.sources.recorders import fireflies

    def boom(self, since=None, cursor=None):
        raise KeyError("a bug in one adapter")
    del fake.fail[("fathom", "fk")]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fireflies.FirefliesRecorder, "list_recent", boom)
        result = connections.poll_user(db, NOW, force=True)
    assert "KeyError" in result["fireflies"]["error"] and result["fathom"]["imported"] == 1


def test_a_transcript_not_ready_is_retried_and_a_colleague_only_meeting_is_not_a_call(db, keys, fake):
    internal = ff("ff-team", [("Maya Iyer", "Standup."), ("Ravi", "Done.")], buyer=("Ravi", "ravi@tessel.test"),
                  organizer="ravi@tessel.test")
    fake.account("fireflies", "good", meetings=[ff("ff-1"), internal])
    fake.not_ready.add("ff-1")
    c = connect(db, "fireflies", "good")
    result = connections.poll_user(db, NOW)["fireflies"]
    assert (result["imported"], result["pending"], result["skipped"]) == (0, 1, 1)
    state = {e["ext_id"]: e["status"] for e in connections.get(db, c["id"])["state"]["recent"]}
    assert state == {"ff-1": "pending", "ff-team": "internal"}
    fake.not_ready.clear()
    assert connections.poll_user(db, NOW + timedelta(hours=1, minutes=1))["fireflies"]["imported"] == 1
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1


def test_a_failing_meeting_is_recorded_and_retried_a_few_times_only(db, keys, fake, monkeypatch):
    fake.account("fireflies", "good", meetings=[ff("ff-bad", [("", "")]), ff("ff-1")])
    c = connect(db, "fireflies", "good")
    result = connections.poll_user(db, NOW)["fireflies"]
    assert result["imported"] == 1 and result["errors"] == 1
    row = connections.get(db, c["id"])
    assert "1 meeting(s) failed" in row["last_error"] and row["status"] == "active"
    bad = next(e for e in row["state"]["recent"] if e["ext_id"] == "ff-bad")
    assert bad["status"] == "failed" and bad["attempts"] == 1
    t = NOW
    for _ in range(connections.MAX_ATTEMPTS + 2):
        t += timedelta(minutes=61)
        connections.poll_user(db, t)
    bad = next(e for e in connections.get(db, c["id"])["state"]["recent"] if e["ext_id"] == "ff-bad")
    assert bad["attempts"] == connections.MAX_ATTEMPTS


def test_the_admins_allow_list_stops_a_kind_for_everyone(db, keys, fake):
    fake.account("fireflies", "good", meetings=[ff("ff-1")])
    connect(db, "fireflies", "good")
    connections.set_allowed_kinds(["fathom", "granola"])
    assert connections.poll_user(db, NOW, force=True) == {}
    ctx = connections.card_context(db)
    assert [c["kind"] for c in ctx["recorder_cards"]] == ["fathom", "granola"]
    assert [c["kind"] for c in ctx["recorder_disallowed"]] == ["fireflies"]
    assert all("secret_enc" not in c for c in ctx["recorder_disallowed"])


def test_the_duty_is_per_user_in_cloud_and_the_org_poller_stays_local(monkeypatch):
    from salescoach.plugins import sources as plugin
    monkeypatch.delenv(identity.MODE_ENV, raising=False)
    assert [(d.name, d.per_user) for d in plugin.background_duties()] == [("sources", False)]
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    assert [(d.name, d.per_user) for d in plugin.background_duties()] == [("recorders", True)]


# ---- uploads in cloud: the acting rep's -------------------------------------------------------------

def test_an_upload_in_cloud_keys_on_the_acting_rep(db, monkeypatch):
    from salescoach.sources.adapters.upload import normalize_file
    export = json.dumps(ff("ff-export")).encode()
    text = b"Maya Iyer: I will send it Friday.\nChen Wu: Good.\n"
    assert normalize_file("upload", export).source_ref == "fireflies:ff-export"               # local: as before
    assert normalize_file("upload", text).source_ref.startswith("upload:local:")
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    with identity.activate(identity.Actor("u-asha", role="rep")):
        assert normalize_file("upload", export).source_ref == "fireflies:u-asha:ff-export"
        assert normalize_file("upload", text).source_ref.startswith("upload:u-asha:")
        generic = json.dumps({"id": "m-1", "turns": [{"speaker": "A", "text": "hi"}]}).encode()
        assert normalize_file("upload", generic).source_ref == "ext:generic:u-asha:m-1"


# ---- Postgres: two reps under the row-level policies ----------------------------------------------

@pytest.fixture
def reps(db):
    for uid, name in (("u-a", "Asha Rao"), ("u-b", "Bala Krishnan"), ("u-c", "Chitra Das")):
        users.create(db, f"{uid[2:]}@tessel.test", name, role="rep", user_id=uid)
    db.commit()
    return ("u-a", "u-b", "u-c")


SAME_MEETING = [("Asha Rao", "I will send the pricing sheet by Friday."),
                ("Bala Krishnan", "And I will loop in our solutions engineer."),
                ("Chen Wu", "We lose two days on every dispute.")]


@pytest.mark.postgres_only
def test_the_same_meeting_in_two_reps_accounts_is_two_independent_calls(db, keys, fake, reps):
    meeting = ff("ff-777", SAME_MEETING, reps_emails=("a@tessel.test", "b@tessel.test"))
    fake.account("fireflies", "key-a", email="a@tessel.test", name="Asha Rao", meetings=[meeting])
    fake.account("fireflies", "key-b", email="b@tessel.test", name="Bala Krishnan", meetings=[meeting])
    for uid, key, name in (("u-a", "key-a", "Asha Rao"), ("u-b", "key-b", "Bala Krishnan")):
        with identity.as_user(db, uid):
            connect(db, "fireflies", key, Account(email=f"{uid[2:]}@tessel.test", name=name))
        with identity.as_user(db, uid, mode=identity.SERVICE):                    # as the duty runs it
            assert connections.poll_user(db, NOW)["fireflies"]["imported"] == 1
    with identity.as_user(db, "u-c", mode=identity.SERVICE):                      # no connection, nothing
        assert connections.poll_user(db, NOW) == {}
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    seen = {}
    for uid, name in (("u-a", "Asha Rao"), ("u-b", "Bala Krishnan")):
        with identity.as_user(db, uid):
            rows = db.execute("SELECT node_id, source_ref, owner_id FROM calls").fetchall()
            assert len(rows) == 1 and rows[0]["owner_id"] == uid and rows[0]["source_ref"] == f"fireflies:{uid}:ff-777"
            mine = db.execute("SELECT text FROM turns WHERE call_id=? AND channel='me'", (rows[0]["node_id"],)).fetchall()
            seen[uid] = [t["text"] for t in mine]
            assert len(connections.list_mine(db)) == 1
    assert seen == {"u-a": ["I will send the pricing sheet by Friday."],
                    "u-b": ["And I will loop in our solutions engineer."]}               # each from their own side


@pytest.mark.postgres_only
def test_rep_b_cannot_see_rep_as_connection_or_meetings(db, keys, fake, reps):
    fake.account("fireflies", "key-a", meetings=[ff("ff-1", SAME_MEETING)])
    with identity.as_user(db, "u-a"):
        a_conn = connect(db, "fireflies", "key-a")
        db.execute("INSERT INTO calendar_meetings(event_id,title,start_at,end_at,attendees,first_seen_at) VALUES "
                   "('ev-a','A only','2026-09-24T10:00:00+00:00','2026-09-24T10:30:00+00:00','[]',?)", (NOW.isoformat(),))
        db.commit()
    with identity.as_user(db, "u-a", mode=identity.SERVICE):
        connections.poll_user(db, NOW)
    with identity.as_user(db, "u-b"):
        assert db.execute("SELECT COUNT(*) FROM source_connections").fetchone()[0] == 0
        assert connections.get(db, a_conn["id"]) is None and connections.list_mine(db) == []
        assert not connections.disconnect(db, a_conn["id"])
        assert connections.new_webhook_token(db, a_conn["id"]) is None
        assert connections.meetings(db, NOW + timedelta(hours=1)) == {"upcoming": [], "recent": []}
        # nor by forging the owner: B's write as A is refused by the policy
        from psycopg import errors
        with pytest.raises(errors.InsufficientPrivilege):
            db.execute("INSERT INTO source_connections(id,owner_id,kind,created_at) VALUES ('rc-x','u-a','tldv',?)",
                       (NOW.isoformat(),))
        db.rollback()
    with identity.as_user(db, "u-a"):
        data = connections.meetings(db, NOW + timedelta(hours=1))
        assert [m["status"] for m in data["recent"]] == ["imported"]


@pytest.mark.postgres_only
def test_one_users_failing_recorder_does_not_stop_the_duty_for_the_next(db, keys, fake, reps, monkeypatch, pg_owner):
    from dataclasses import replace

    from conftest import seed_org_settings
    from salescoach.automation import scheduler
    from salescoach.plugins import sources as plugin
    from salescoach.store import stores
    fake.account("fireflies", "key-a", meetings=[ff("ff-1", SAME_MEETING)])
    fake.account("fireflies", "key-b", meetings=[ff("ff-1", SAME_MEETING)])
    for uid, key in (("u-a", "key-a"), ("u-b", "key-b")):
        with identity.as_user(db, uid):
            connect(db, "fireflies", key)
    fake.fail[("fireflies", "key-a")] = (500, {})
    seed_org_settings(pg_owner)
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    duty = replace(plugin.background_duties()[0], first_delay_s=0)

    class Stop:
        def __init__(self):
            self.waits = 0

        def wait(self, delay):
            self.waits += 1
            return self.waits > 1

    scheduler._loop(duty, stores.db_path(), Stop())
    rows = {r["owner_id"]: r for r in pg_owner.execute("SELECT owner_id, status, last_error, failures FROM source_connections")}
    assert "HTTP 500" in rows["u-a"]["last_error"] and rows["u-a"]["failures"] == 1
    assert rows["u-b"]["last_error"] is None and rows["u-b"]["failures"] == 0
    owners = [r["owner_id"] for r in pg_owner.execute("SELECT owner_id FROM calls")]
    assert owners == ["u-b"]
