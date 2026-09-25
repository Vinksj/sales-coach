"""The per-rep recorder adapters (sources/recorders), each against a fake of its API (httpx.MockTransport).

Pinned per adapter: it lists THAT account's meetings (the rep's own: Fireflies mine:true, tl;dv
onlyParticipated, Fathom's shared recordings of colleagues skipped), pages through the listing and says
where a capped listing stopped, maps the transcript with the rep's own turns on channel 'me' where the
recorder says so (Fathom's matched e-mail, Granola's attribution, Fireflies' account name), embeds the
owner in every reference, answers 401 with SourceAuthError and 429 with SourceRateLimited (Retry-After
kept), and names the meeting a webhook announces. The Standard Webhooks signature check. No network.
"""
from datetime import datetime, timezone

import pytest

from salescoach.sources import recorders
from salescoach.sources.adapters import SourceAuthError, SourceNotFound, SourceRateLimited
from salescoach.sources.recorders import Account, signatures

import recorder_fakes as rf

SINCE = datetime(2026, 9, 22, tzinfo=timezone.utc)
OWNER = "u-asha"


def build(fake, kind, key="key-a", account=None):
    return recorders.build(kind, key, OWNER, transport=fake.transport(), account=account)


def test_catalog_every_adapter_is_unverified_and_has_the_research_facts():
    classes = recorders.classes()
    assert list(classes) == ["fathom", "fireflies", "tldv", "granola"]
    for cls in classes.values():
        assert cls.verified is False and cls.where_key and cls.plan and cls.default_poll_minutes >= 15
    assert classes["fireflies"].default_poll_minutes == 60                   # Free plan: 50 requests a day
    assert {k for k, c in classes.items() if c.webhook_scheme == "standard"} == {"fathom", "granola"}


def test_an_adapter_needs_a_key_and_an_owner_and_never_shows_the_key():
    with pytest.raises(SourceAuthError):
        recorders.build("fathom", "", OWNER)
    with pytest.raises(ValueError):
        recorders.build("fathom", "k", "")
    adapter = recorders.build("fathom", "super-secret-key", OWNER)
    assert "super-secret-key" not in repr(adapter) and "super-secret-key" not in str(vars(adapter).get("owner"))


# ---- Fathom ---------------------------------------------------------------------------------------

def test_fathom_lists_pages_maps_and_marks_the_reps_own_turns(monkeypatch):
    fake = rf.FakeRecorders(page_size=2)
    mine = [rf.fathom_meeting(f"rec-{i}", "asha@tessel.test", "Asha Rao", when=f"2026-09-24T1{i}:00:00Z") for i in range(3)]
    colleague = rf.fathom_meeting("rec-shared", "bala@tessel.test", "Bala K")          # shared with Asha, not hers
    fake.account("fathom", "key-a", meetings=mine + [colleague])
    adapter = build(fake, "fathom", account=Account(email="asha@tessel.test"))
    refs = adapter.list_recent(SINCE)
    assert [r.ext_id for r in refs] == ["rec-0", "rec-1", "rec-2"]                  # two pages; the colleague's skipped
    assert [r.source_ref for r in refs][0] == "fathom:u-asha:rec-0"
    assert "chen@buyer.example" in refs[0].emails and refs[0].started_at == "2026-09-24T10:00:00+00:00"
    calls = [p for (k, key, m, path, p) in fake.requests if path.endswith("/meetings")]
    assert calls[0]["include_transcript"] == "true" and calls[0]["created_after"] == "2026-09-22T00:00:00Z"
    assert calls[1]["cursor"] == "2"                                               # the cursor is followed
    nt = adapter.fetch("rec-1")
    assert nt.source_ref == "fathom:u-asha:rec-1" and nt.source_kind == "fathom"
    assert [t.get("channel") for t in nt.turns] == ["me", None]                     # matched e-mail = the recorder
    assert {"name": "Chen Wu", "email": "chen@buyer.example"} in nt.participants
    assert not any(path.endswith("/transcript") for (_, _, _, path, _) in fake.requests)   # the listing carried it


def test_fathom_capped_listing_says_where_it_stopped(monkeypatch):
    fake = rf.FakeRecorders(page_size=1)
    fake.account("fathom", "key-a", meetings=[rf.fathom_meeting(f"r{i}", "asha@tessel.test", "Asha Rao") for i in range(12)])
    monkeypatch.setattr(recorders, "MAX_PAGES", 10)
    from salescoach.sources.recorders import fathom
    monkeypatch.setattr(fathom, "MAX_PAGES", 3)
    adapter = build(fake, "fathom")
    assert len(adapter.list_recent(SINCE)) == 3 and adapter.cursor == "3"
    assert [r.ext_id for r in adapter.list_recent(SINCE, adapter.cursor)] == ["r3", "r4", "r5"]


def test_fathom_test_401_429_and_the_transcript_fallback():
    fake = rf.FakeRecorders()
    fake.account("fathom", "key-a", meetings=[rf.fathom_meeting("rec-1", "asha@tessel.test", "Asha Rao")])
    assert build(fake, "fathom").test() == Account(email="asha@tessel.test", name="Asha Rao")
    with pytest.raises(SourceAuthError) as refused:
        build(fake, "fathom", key="wrong-key").test()
    assert "wrong-key" not in str(refused.value)
    fake.fail[("fathom", "key-a")] = (429, {"retry-after": "90"})
    with pytest.raises(SourceRateLimited) as limited:
        build(fake, "fathom").list_recent(SINCE)
    assert limited.value.retry_after == 90
    del fake.fail[("fathom", "key-a")]
    nt = build(fake, "fathom").fetch("rec-1")                                      # not listed: the transcript endpoint
    assert nt.source_ref == "fathom:u-asha:rec-1" and len(nt.turns) == 2
    fake.not_ready.add("rec-1")
    with pytest.raises(SourceNotFound):
        build(fake, "fathom").fetch("rec-1")


def test_fathom_webhook_payload_embeds_the_transcript_and_names_no_owner():
    fake = rf.FakeRecorders()
    adapter = build(fake, "fathom", account=Account(email="asha@tessel.test"))
    payload = {**rf.fathom_meeting("rec-9", "asha@tessel.test", "Asha Rao"), "owner": "u-bala", "owner_id": "u-bala"}
    ext_id, nt = adapter.webhook_event(payload)
    assert ext_id == "rec-9" and nt.source_ref == "fathom:u-asha:rec-9"             # the adapter's owner, always
    assert adapter.webhook_event({"nothing": 1}) == (None, None)
    assert adapter.webhook_event({"recording_id": "../../etc"}) == (None, None)


# ---- Fireflies ------------------------------------------------------------------------------------

def _ff(tid, **kw):
    return rf.fireflies_transcript(tid, [("Asha Rao", "I will send the pricing sheet by Friday."),
                                         ("Chen Wu", "We lose two days on every dispute.")], **kw)


def test_fireflies_lists_mine_pages_by_skip_and_marks_the_account_holder(monkeypatch):
    from salescoach.sources.recorders import fireflies
    monkeypatch.setattr(fireflies, "PAGE", 2)
    fake = rf.FakeRecorders()
    fake.account("fireflies", "key-a", email="asha@tessel.test", name="Asha Rao",
                 meetings=[_ff(f"ff-{i}") for i in range(3)])
    adapter = build(fake, "fireflies")
    account = adapter.test()
    assert account == Account(email="asha@tessel.test", name="Asha Rao")
    adapter = build(fake, "fireflies", account=account)
    refs = adapter.list_recent(SINCE)
    assert [r.ext_id for r in refs] == ["ff-0", "ff-1", "ff-2"] and refs[0].source_ref == "fireflies:u-asha:ff-0"
    assert "chen@buyer.example" in refs[0].emails
    listing = [p for (_, _, _, _, p) in fake.requests if p == {}]
    assert len(listing) == 1 + 2                                                    # user + two pages (skip 0, 2)
    nt = adapter.fetch("ff-1")
    assert nt.source_ref == "fireflies:u-asha:ff-1"
    assert [(t["speaker_label"], t.get("channel")) for t in nt.turns] == [("Asha Rao", "me"), ("Chen Wu", None)]


def test_fireflies_rate_limits_by_status_and_by_graphql_error_and_refuses_a_bad_key():
    fake = rf.FakeRecorders()
    fake.account("fireflies", "key-a", email="asha@tessel.test", name="Asha Rao", meetings=[_ff("ff-1")])
    fake.fail[("fireflies", "key-a")] = (429, {"retry-after": "600"})
    with pytest.raises(SourceRateLimited) as limited:
        build(fake, "fireflies").list_recent(SINCE)
    assert limited.value.retry_after == 600
    del fake.fail[("fireflies", "key-a")]

    class Limited(rf.FakeRecorders):
        def fireflies(self, request, acct, params):
            import httpx
            return httpx.Response(200, json={"errors": [{"message": "Too many requests", "code": "too_many_requests"}]})
    other = Limited()
    other.account("fireflies", "key-a")
    with pytest.raises(SourceRateLimited):
        build(other, "fireflies").list_recent(SINCE)
    with pytest.raises(SourceAuthError):
        build(fake, "fireflies", key="nope").test()
    fake.not_ready.add("ff-1")
    with pytest.raises(SourceNotFound):
        build(fake, "fireflies").fetch("ff-1")


def test_fireflies_webhook_is_a_notification():
    adapter = build(rf.FakeRecorders(), "fireflies")
    assert adapter.webhook_event({"meetingId": "ff-7", "eventType": "Transcription completed"}) == ("ff-7", None)
    assert adapter.webhook_event({"meetingId": "a b"}) == (None, None)


# ---- tl;dv ---------------------------------------------------------------------------------------

def test_tldv_lists_participated_pages_and_maps_the_transcript():
    fake = rf.FakeRecorders(page_size=2)
    fake.account("tldv", "key-a", meetings=[rf.tldv_meeting(f"tl-{i}", "Asha Rao", "asha@tessel.test") for i in range(3)])
    adapter = build(fake, "tldv", account=Account(name="Asha Rao"))
    assert adapter.test() == Account()
    refs = adapter.list_recent(SINCE)
    assert [r.ext_id for r in refs] == ["tl-0", "tl-1", "tl-2"] and refs[2].source_ref == "tldv:u-asha:tl-2"
    listing = [p for (_, _, _, path, p) in fake.requests if path == "/v1alpha1/meetings" and "page" in p]
    assert [p["page"] for p in listing] == ["1", "2"] and listing[0]["from"] == "2026-09-22T00:00:00Z"
    nt = adapter.fetch("tl-1")
    assert nt.source_ref == "tldv:u-asha:tl-1" and nt.title == "Buyer discovery"
    assert [(t["speaker_label"], t.get("channel")) for t in nt.turns] == [("Asha Rao", "me"), ("Chen Wu", None)]
    assert nt.turns[1]["t_start"] == 10
    fake.not_ready.add("tl-2")
    with pytest.raises(SourceNotFound):
        adapter.fetch("tl-2")
    with pytest.raises(SourceAuthError):
        build(fake, "tldv", key="nope").list_recent(SINCE)
    fake.fail[("tldv", "key-a")] = (429, {})
    with pytest.raises(SourceRateLimited) as limited:
        adapter.list_recent(SINCE)
    assert limited.value.retry_after is None
    assert adapter.webhook_event({"event": "TranscriptReady", "data": {"meetingId": "tl-5", "id": "tr-5"}}) == ("tl-5", None)


# ---- Granola --------------------------------------------------------------------------------------

def test_granola_maps_attribution_me_them_directly():
    fake = rf.FakeRecorders(page_size=1)
    fake.account("granola", "key-a", meetings=[rf.granola_note(f"not_{i}", "Asha Rao", "asha@tessel.test") for i in range(2)])
    adapter = build(fake, "granola")
    assert adapter.test() == Account(email="asha@tessel.test", name="Asha Rao")
    refs = adapter.list_recent(SINCE)
    assert [r.ext_id for r in refs] == ["not_0", "not_1"] and refs[0].source_ref == "granola:u-asha:not_0"
    nt = adapter.fetch("not_1")
    assert [(t["speaker_label"], t["channel"]) for t in nt.turns] == [("Me", "me"), ("Them", "them")]
    assert nt.summary == "Discovery call." and {"name": "Chen Wu", "email": "chen@buyer.example"} in nt.participants
    fetches = [p for (_, _, _, path, p) in fake.requests if path == "/v1/notes/not_1"]
    assert fetches == [{"include": "transcript"}]
    fake.not_ready.add("not_0")
    with pytest.raises(SourceNotFound):
        adapter.fetch("not_0")
    with pytest.raises(SourceAuthError):
        build(fake, "granola", key="nope").test()
    assert adapter.webhook_event({"type": "note.generated", "data": {"id": "not_7"}}) == ("not_7", None)


# ---- signatures -----------------------------------------------------------------------------------

def test_standard_webhooks_signature_fresh_exact_and_constant():
    import base64
    secret = "whsec_" + base64.b64encode(b"k" * 24).decode()
    body = b'{"type":"note.generated","data":{"id":"not_1"}}'
    now = 1_790_000_000
    sig = signatures.standard_sign(secret, "msg_1", str(now), body)
    headers = {"webhook-id": "msg_1", "webhook-timestamp": str(now), "webhook-signature": "v1,bogus " + sig}
    assert signatures.standard_ok(secret, headers, body, now=now)
    assert not signatures.standard_ok(secret, headers, body + b" ", now=now)                  # the exact body
    assert not signatures.standard_ok(secret, headers, body, now=now + 301)                   # stale
    assert not signatures.standard_ok("whsec_" + base64.b64encode(b"x" * 24).decode(), headers, body, now=now)
    assert not signatures.standard_ok(None, headers, body, now=now)
    assert not signatures.standard_ok(secret, {**headers, "webhook-id": "msg_2"}, body, now=now)
    assert signatures.token_ok("t0k", signatures.token_hash("t0k")) and not signatures.token_ok("t0k", None)
    assert not signatures.token_ok("", signatures.token_hash(""))
