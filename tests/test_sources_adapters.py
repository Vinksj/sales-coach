"""Adapters, settings and the poller. No network: Fireflies and Fathom run on httpx.MockTransport."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from salescoach import config, providers, repo, sources
from salescoach.sources import adapters, base
from salescoach.sources.adapters import SourceAuthError, SourceError
from salescoach.sources.adapters.fathom import FathomAdapter
from salescoach.sources.adapters.fireflies import PAGE, FirefliesAdapter
from salescoach.sources.adapters.folder import FolderAdapter
from salescoach.sources.adapters.granola import GranolaAdapter
from test_sources_support import FATHOM_MEETING, FATHOM_TRANSCRIPT, FIREFLIES, NAMED, generic_bytes

SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _old(path):
    past = time.time() - 60
    os.utime(path, (past, past))
    return path


@pytest.fixture
def drop(db):
    folder = config.DATA_DIR / "inbox" / "drop"
    folder.mkdir(parents=True)
    return folder


# ---- protocol and catalog ------------------------------------------------------------------------

def test_every_adapter_satisfies_the_protocol():
    for kind, cls in adapters.classes().items():
        adapter = cls()
        assert isinstance(adapter, adapters.SourceAdapter) and adapter.kind == kind and adapter.how
    assert list(adapters.classes()) == ["upload", "folder", "webhook", "fireflies", "fathom", "granola"]
    with pytest.raises(SourceError):
        adapters.build("otter")                       # no invented adapter for a service without a public API


def test_catalog_descriptors(db, monkeypatch):
    monkeypatch.setattr(providers, "claude_cli_available", lambda: False)
    cat = {d["kind"]: d for d in sources.catalog(db)}
    assert set(cat["fireflies"]) == {"kind", "label", "how", "mode", "needs_key", "api_key_env", "verified",
                                     "configured", "enabled", "poll_minutes", "options", "last_run", "last_error"}
    assert (cat["upload"]["mode"], cat["upload"]["enabled"], cat["upload"]["configured"]) == ("push", True, True)
    assert (cat["folder"]["enabled"], cat["folder"]["poll_minutes"]) == (True, 1)
    assert (cat["webhook"]["api_key_env"], cat["webhook"]["configured"]) == ("WEBHOOK_SECRET", False)
    for kind, env in (("fireflies", "FIREFLIES_API_KEY"), ("fathom", "FATHOM_API_KEY")):
        d = cat[kind]
        assert (d["needs_key"], d["api_key_env"], d["verified"], d["configured"], d["enabled"]) == \
            (True, env, False, False, False)
        assert "ntested against the live API" in d["how"]
    assert cat["granola"]["configured"] is False and cat["granola"]["verified"] is True
    for kind in ("otter", "tldv", "zoom", "meet", "teams", "gong"):
        assert cat[kind]["mode"] == "export" and "pload" in cat[kind]["how"]
    config.set_secret("FIREFLIES_API_KEY", "ff-test-key")
    fresh = {d["kind"]: d for d in sources.catalog(db)}
    assert fresh["fireflies"]["configured"] is True
    assert "ff-test-key" not in json.dumps(fresh)


def test_save_takes_effect_and_validates(db):
    d = sources.save("fireflies", True, poll_minutes=30, options={"only_deals": True})
    assert (d["enabled"], d["poll_minutes"], d["options"]) == (True, 30, {"only_deals": True})
    sources.save("folder", False)
    assert sources.settings()["folder"]["enabled"] is False and sources.settings()["fireflies"]["poll_minutes"] == 30
    assert sources.save("upload", False)["enabled"] is True                 # the import page cannot be switched off
    assert sources.save("webhook", True)["poll_minutes"] is None
    saved = config.load_user("sources")["sources"]
    assert [e["kind"] for e in saved] == ["fireflies", "folder", "upload", "webhook"]
    for bad in (("otter", True), ("nope", True)):
        with pytest.raises(ValueError):
            sources.save(*bad)
    with pytest.raises(ValueError):
        sources.save("fathom", True, poll_minutes=0)
    with pytest.raises(ValueError):
        sources.save("fathom", True, poll_minutes="soon")
    with pytest.raises(ValueError):
        sources.save("fathom", True, options={"nested": {"a": 1}})


def test_saving_a_source_keeps_the_remembered_labels(db):
    base.remember_me_label("Priya S.")
    sources.save("fathom", True)
    assert config.load_user("sources")["me_labels"] == ["Priya S."]


def test_new_webhook_secret_is_stored_not_logged(db):
    from salescoach.sources.adapters import webhook
    assert not webhook.authorised("anything")
    value = sources.new_webhook_secret()
    assert len(value) >= 40 and webhook.authorised(value) and not webhook.authorised(value + "x")
    assert not webhook.authorised("") and not webhook.authorised(None)


# ---- folder --------------------------------------------------------------------------------------

def test_folder_imports_once_moves_files_and_records_errors(db, drop):
    (drop / "acme weekly.txt").write_text(NAMED)
    (drop / "zap.json").write_bytes(generic_bytes())
    (drop / "notes.txt").write_text("Just some notes with nobody labelled.")
    (drop / "strangers.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nPriya S.: I'll send pricing.\n\n2\n00:00:03,000 --> 00:00:04,000\n"
        "Dev Rao: Good.\n")
    (drop / ".DS_Store").write_text("x")
    (drop / "photo.png").write_bytes(b"\x89PNG")
    for p in drop.iterdir():
        _old(p)
    (drop / "still-writing.txt").write_text("Maya Iyer: half a")          # fresh mtime: left for the next round

    result = sources.poll(db, kinds=["folder"], force=True)["folder"]
    assert len(result["imported"]) == 2 and len(result["needs_speaker"]) == 1 and len(result["errors"]) == 1
    assert sorted(p.name for p in (drop / "processed").iterdir()) == ["acme weekly.txt", "strangers.srt", "zap.json"]
    # Review 3: a file that is not a transcript is LEFT WHERE IT IS (it is somebody's file), and remembered
    # by content hash so the next poll does not parse it again. Nothing is moved to failed/ any more.
    assert not (drop / "failed").exists()
    assert sorted(p.name for p in drop.iterdir() if p.is_file()) == [".DS_Store", "notes.txt", "photo.png",
                                                                     "still-writing.txt"]
    skipped = json.loads(db.execute("SELECT value FROM state WHERE key='sources:folder:skipped'").fetchone()[0])
    assert [(e["name"], "no speaker turns found" in e["error"]) for e in skipped.values()] == [("notes.txt", True)]
    calls = {r["title"]: r for r in db.execute("SELECT * FROM calls")}
    assert set(calls) == {"acme weekly", "Acme discovery", "strangers"}
    assert calls["acme weekly"]["source"] == "folder" and calls["acme weekly"]["history"] == 0
    assert calls["strangers"]["wf_state"] == "needs_speaker"
    assert json.loads(db.execute("SELECT value FROM state WHERE key='sources:folder:last_error'").fetchone()[0])["error"]

    # the same content dropped again under another name: no second call, and the file still leaves the folder
    (drop / "copy of acme.txt").write_text(NAMED)
    _old(drop / "copy of acme.txt")
    again = sources.poll(db, kinds=["folder"], force=True)["folder"]
    assert again["imported"] == [] and again["skipped"] == 1
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 3
    assert (drop / "processed" / "copy of acme.txt").exists()
    assert db.execute("SELECT value FROM state WHERE key='sources:folder:last_error'").fetchone()[0] == ""


def test_folder_never_reads_outside_the_drop_folder(db, drop, tmp_path):
    (tmp_path / "secret.txt").write_text("Me: x\nThem: y")
    for name in ("../secret.txt", "/etc/hosts", "missing.txt"):
        with pytest.raises(SourceError):
            FolderAdapter().fetch(name)
    (drop / "link.txt").symlink_to(tmp_path / "secret.txt")
    assert FolderAdapter().list_recent() == []


def test_folder_maps_the_deal_from_participants(db, drop):
    acct = repo.create_account(db, "Acme Freight", ["acmefreight.test"])
    deal = repo.create_deal(db, "Acme pilot", account_id=acct)
    db.commit()
    _old_file = drop / "zap.json"
    _old_file.write_bytes(generic_bytes())
    _old(_old_file)
    call = sources.poll(db, kinds=["folder"], force=True)["folder"]["imported"][0]
    assert repo.get_call(db, call)["deal_id"] == deal
    asha = repo.find_person_by_email(db, "asha.rao@acmefreight.test")
    assert db.execute("SELECT 1 FROM deal_people WHERE deal_id=? AND person_id=?", (deal, asha)).fetchone()


# ---- fireflies -----------------------------------------------------------------------------------

def fireflies_transport(log, pages=None, fail=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        log.append({"auth": request.headers.get("authorization"), "url": str(request.url), **body})
        if fail:
            return fail
        if "transcripts(" in body["query"]:
            skip = body["variables"]["skip"]
            return httpx.Response(200, json={"data": {"transcripts": (pages or [[]])[skip // PAGE]}})
        return httpx.Response(200, json={"data": {"transcript": {**FIREFLIES, "id": body["variables"]["id"]}}})
    return httpx.MockTransport(handler)


def _ff_item(i):
    return {"id": f"ff-{i}", "title": f"Meeting {i}", "date": 1788343200000 + i, "participants": [],
            "meeting_attendees": [{"displayName": "Asha Rao", "email": "Asha.Rao@acmefreight.test"}]}


def test_fireflies_lists_with_pagination_and_maps_a_transcript(db):
    config.set_secret("FIREFLIES_API_KEY", "ff-test-key")
    log = []
    pages = [[_ff_item(i) for i in range(PAGE)], [_ff_item(i) for i in range(PAGE, PAGE + 3)]]
    adapter = FirefliesAdapter(transport=fireflies_transport(log, pages))
    refs = adapter.list_recent(SINCE)
    assert len(refs) == PAGE + 3 and [c["variables"]["skip"] for c in log] == [0, PAGE]
    assert log[0]["url"] == "https://api.fireflies.ai/graphql" and log[0]["auth"] == "Bearer ff-test-key"
    assert log[0]["variables"]["fromDate"] == "2026-09-01T00:00:00.000Z"
    assert (refs[0].ext_id, refs[0].source_ref, refs[0].emails) == ("ff-0", "fireflies:ff-0", ["asha.rao@acmefreight.test"])
    nt = adapter.fetch("ff-7")
    assert (nt.source_kind, nt.source_ref, nt.title) == ("fireflies", "fireflies:ff-7", "Acme <> Tessel weekly")
    assert [t["speaker_label"] for t in nt.turns] == ["Asha Rao", "Maya Iyer", "Asha Rao"]
    assert nt.turns[1]["t_start"] == 5.5 and nt.summary == "Asha wants lane data first."
    out = base.import_normalized(db, nt, add_me=True)
    assert [t["channel"] for t in repo.turns(db, out.call_id)] == ["them", "me", "them"]


@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"message": "Unauthorized"}),
    httpx.Response(200, json={"errors": [{"message": "Invalid API key", "code": "auth_failed"}], "data": None}),
])
def test_fireflies_auth_failure_names_no_key(db, response):
    config.set_secret("FIREFLIES_API_KEY", "ff-test-key")
    with pytest.raises(SourceAuthError) as err:
        FirefliesAdapter(transport=fireflies_transport([], fail=response)).list_recent(SINCE)
    assert "ff-test-key" not in str(err.value) and err.value.__cause__ is None


def test_fireflies_other_failures(db):
    with pytest.raises(SourceAuthError):
        FirefliesAdapter(transport=fireflies_transport([])).list_recent(SINCE)          # no key set: no request
    config.set_secret("FIREFLIES_API_KEY", "ff-test-key")
    for response, needle in ((httpx.Response(429), "rate limiting"), (httpx.Response(500), "HTTP 500"),
                             (httpx.Response(200, text="<html>"), "JSON"),
                             (httpx.Response(302, headers={"location": "https://evil.test/"}), "HTTP 302"),
                             (httpx.Response(200, json={"errors": [{"message": "too complex"}]}), "too complex")):
        with pytest.raises(SourceError) as err:
            FirefliesAdapter(transport=fireflies_transport([], fail=response)).list_recent(SINCE)
        assert needle in str(err.value) and "ff-test-key" not in str(err.value)

    def boom(request):
        raise httpx.ConnectError("no route", request=request)
    with pytest.raises(SourceError) as err:
        FirefliesAdapter(transport=httpx.MockTransport(boom)).fetch("x")
    assert "could not be reached" in str(err.value) and err.value.__cause__ is None


# ---- fathom --------------------------------------------------------------------------------------

def fathom_transport(log, fail=None):
    pages = {None: {"items": [FATHOM_MEETING, {**FATHOM_MEETING, "recording_id": 4471204, "title": "Second"}],
                    "next_cursor": "c2", "limit": 2},
             "c2": {"items": [{**FATHOM_MEETING, "recording_id": 4471205, "title": "Third"}], "next_cursor": None}}

    def handler(request: httpx.Request) -> httpx.Response:
        log.append({"key": request.headers.get("x-api-key"), "path": request.url.path,
                    "params": dict(request.url.params)})
        if fail:
            return fail
        if request.url.path.endswith("/meetings"):
            return httpx.Response(200, json=pages[request.url.params.get("cursor")])
        return httpx.Response(200, json={"transcript": FATHOM_TRANSCRIPT})
    return httpx.MockTransport(handler)


def test_fathom_lists_with_cursor_pagination_and_maps_a_meeting(db):
    config.set_secret("FATHOM_API_KEY", "fathom-test-key")
    log = []
    adapter = FathomAdapter(transport=fathom_transport(log))
    refs = adapter.list_recent(SINCE)
    assert [r.ext_id for r in refs] == ["4471203", "4471204", "4471205"]
    assert [c["params"].get("cursor") for c in log] == [None, "c2"] and log[0]["key"] == "fathom-test-key"
    assert log[0]["params"]["created_after"] == "2026-09-01T00:00:00Z"
    assert log[0]["path"] == "/external/v1/meetings"
    assert refs[0].source_ref == "fathom:4471203" and "asha.rao@acmefreight.test" in refs[0].emails
    nt = adapter.fetch("4471203")
    assert log[-1]["path"] == "/external/v1/recordings/4471203/transcript"
    assert (nt.source_ref, nt.title, nt.started_at) == ("fathom:4471203", "Acme pilot review", "2026-09-16T09:31:10+00:00")
    # "S. Jain" is not a name the profile knows, but Fathom matched that speaker to the seller's address
    out = base.import_normalized(db, nt, add_me=True)
    assert not out.needs_speaker
    assert [t["channel"] for t in repo.turns(db, out.call_id)] == ["them", "me", "them"]
    assert [t["t_start"] for t in repo.turns(db, out.call_id)] == [4.0, 11.0, 62.0]
    art = db.execute("SELECT json FROM artifacts WHERE call_id=? AND kind='fathom_summary'", (out.call_id,)).fetchone()
    assert json.loads(art[0])["trust"] == "third_party_inference"


def test_fathom_auth_failure_and_bad_ids(db):
    with pytest.raises(SourceAuthError):
        FathomAdapter(transport=fathom_transport([])).list_recent(SINCE)
    config.set_secret("FATHOM_API_KEY", "fathom-test-key")
    with pytest.raises(SourceAuthError) as err:
        FathomAdapter(transport=fathom_transport([], fail=httpx.Response(403))).list_recent(SINCE)
    assert "fathom-test-key" not in str(err.value)
    log = []
    with pytest.raises(SourceError):
        FathomAdapter(transport=fathom_transport(log)).fetch("../../meetings")
    assert log == []


# ---- granola wrapper -----------------------------------------------------------------------------

def test_granola_adapter_is_gated_on_the_cli_and_wraps_the_existing_path(db, monkeypatch):
    from salescoach.sources import granola
    monkeypatch.setattr(providers, "claude_cli_available", lambda: False)
    assert GranolaAdapter().configured() is False
    monkeypatch.setattr(providers, "claude_cli_available", lambda: True)
    assert GranolaAdapter().configured() is True
    mid = "11111111-2222-3333-4444-555555555555"
    meeting = {"id": mid, "title": "Asha <> Maya", "date": "Sep 16, 2026 2:00 PM GMT+5:30", "summary": None,
               "participants": [{"name": "Asha Rao", "email": "asha.rao@acmefreight.test", "org": ""}]}
    asked = []
    monkeypatch.setattr(granola, "list_meetings", lambda r: asked.append(r) or [meeting])
    monkeypatch.setattr(granola, "fetch", lambda m: {**meeting, "transcript": " Them: Hello.  Me: Hi."})
    refs = GranolaAdapter().list_recent(datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert asked == ["last_30_days"] and refs[0].source_ref == f"granola:{mid}"
    assert GranolaAdapter().list_recent(datetime(2026, 9, 17, tzinfo=timezone.utc)) == []      # older than `since`
    sources.save("granola", True)
    result = sources.poll(db, kinds=["granola"], force=True, moment=datetime(2026, 9, 17, tzinfo=timezone.utc))
    call = result["granola"]["imported"][0]
    row = repo.get_call(db, call)
    assert (row["source"], row["history"], row["source_ref"]) == ("granola", 0, f"granola:{mid}")   # polled = not history


# ---- the poller ----------------------------------------------------------------------------------

class Stub(adapters.Adapter):
    def __init__(self, kind, refs=(), fetch=None, configured=True, list_error=None):
        super().__init__()
        self.kind, self._refs, self._fetch, self._ok, self._list_error = kind, list(refs), fetch, configured, list_error
        self.listed, self.fetched = [], []

    def configured(self):
        return self._ok

    def list_recent(self, since=None):
        self.listed.append(since)
        if self._list_error:
            raise self._list_error
        return self._refs

    def fetch(self, ext_id):
        self.fetched.append(ext_id)
        return self._fetch(ext_id)


def _nt(kind, ext_id, text=NAMED, emails=("asha.rao@acmefreight.test",)):
    from salescoach.sources import parsers
    nt = base.from_parsed(parsers.parse_plain(text + f"\nArjun Kumar: ref {ext_id}."), kind, raw={"id": ext_id},
                          source_ref=f"{kind}:{ext_id}", title=f"Meeting {ext_id}")
    nt.participants = [{"name": "Asha Rao", "email": e} for e in emails]
    return nt


def _ref(kind, ext_id, emails=("asha.rao@acmefreight.test",)):
    return adapters.MeetingRef(ext_id=ext_id, source_ref=f"{kind}:{ext_id}", emails=list(emails))


def test_one_adapters_failure_never_stops_the_others(db):
    sources.save("fireflies", True)
    sources.save("fathom", True)
    sources.save("folder", False)
    broken = Stub("fireflies", list_error=SourceAuthError("Fireflies refused the API key; set a new key"))
    good = Stub("fathom", [_ref("fathom", "1"), _ref("fathom", "bad"), _ref("fathom", "2")],
                fetch=lambda i: (_ for _ in ()).throw(SourceError("transcript not ready")) if i == "bad"
                else _nt("fathom", i))
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    out = sources.poll(db, adapters={"fireflies": broken, "fathom": good}, moment=now)
    assert "refused the API key" in out["fireflies"]["error"]
    assert len(out["fathom"]["imported"]) == 2 and out["fathom"]["errors"][0]["id"] == "bad"
    state = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM state WHERE key LIKE 'sources:%'")}
    assert "refused the API key" in json.loads(state["sources:fireflies:last_error"])["error"]
    assert "sources:fireflies:last_ok" not in state and state["sources:fireflies:last_run"] == now.isoformat(timespec="seconds")
    assert "transcript not ready" in json.loads(state["sources:fathom:last_error"])["error"]
    assert state["sources:fathom:last_ok"] == now.isoformat(timespec="seconds")
    cat = {d["kind"]: d for d in sources.catalog(db)}
    assert "refused" in cat["fireflies"]["last_error"]["error"] and cat["fathom"]["last_run"]
    # imported recorder calls are NOT history: a follow-up will be drafted
    assert {r["history"] for r in db.execute("SELECT history FROM calls")} == {0}
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='CALL_ENDED'").fetchone()[0] == 2


def test_polling_is_idempotent_due_aware_and_windowed(db):
    sources.save("fathom", True, poll_minutes=15)
    sources.save("folder", False)
    good = Stub("fathom", [_ref("fathom", "1")], fetch=lambda i: _nt("fathom", i))
    t0 = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    assert len(sources.poll(db, adapters={"fathom": good}, moment=t0)["fathom"]["imported"]) == 1
    assert good.listed == [t0 - timedelta(days=sources.FIRST_LOOKBACK_DAYS)]
    assert sources.poll(db, adapters={"fathom": good}, moment=t0 + timedelta(minutes=5)) == {}      # not due yet
    again = sources.poll(db, adapters={"fathom": good}, moment=t0 + timedelta(minutes=15))["fathom"]
    assert again["imported"] == [] and again["skipped"] == 1 and good.fetched == ["1"]             # known ref: no fetch
    assert good.listed[-1] == t0 - sources.OVERLAP
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1


def test_disabled_unconfigured_internal_and_only_deals(db):
    stub = Stub("fathom", [_ref("fathom", "1")], fetch=lambda i: _nt("fathom", i))
    assert sources.poll(db, adapters={"fathom": stub}, force=True).get("fathom") is None           # disabled by default
    sources.save("fathom", True)
    off = Stub("fathom", [_ref("fathom", "1")], configured=False)
    assert sources.poll(db, adapters={"fathom": off}, force=True)["fathom"] == {"skipped": "not configured"}
    team = Stub("fathom", [_ref("fathom", "t", emails=("maya@tessel.test", "piyush@tessel.test"))],
                fetch=lambda i: _nt("fathom", i))
    assert sources.poll(db, adapters={"fathom": team}, force=True)["fathom"]["skipped"] == 1 and team.fetched == []
    sources.save("fathom", True, options={"only_deals": True})
    assert sources.poll(db, adapters={"fathom": stub}, force=True)["fathom"]["skipped"] == 1       # no known deal
    acct = repo.create_account(db, "Acme Freight", ["acmefreight.test"])
    deal = repo.create_deal(db, "Acme pilot", account_id=acct)
    db.commit()
    call = sources.poll(db, adapters={"fathom": stub}, force=True)["fathom"]["imported"][0]
    assert repo.get_call(db, call)["deal_id"] == deal


def test_the_scheduler_duty(db, monkeypatch, drop):
    from salescoach.automation import scheduler
    from salescoach.plugins import sources as plugin
    started = []
    monkeypatch.setattr(scheduler, "start", lambda db_path, stop, duties=None: started.extend(duties))
    plugin.start_background("x.db", object())
    assert [(d.name, d.interval_s()) for d in started] == [("sources", 60)]
    (drop / "a.txt").write_text(NAMED)
    _old(drop / "a.txt")
    sources.save("fireflies", True)                       # enabled, but no key: skipped, and nothing raises
    summary = started[0].run(db)
    assert summary["folder"]["imported"] == 1 and summary["fireflies"] == {"skipped": "not configured"}
