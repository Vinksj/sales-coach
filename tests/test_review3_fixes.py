"""Review 3 (2026-09-18) regression tests. One section per finding; each asserts the FIXED behaviour.

  1  parser DoS (regex, size cap, event loop, .processing)     6  migration 4's foreign-key check
  2  learning verdicts gate the legacy seller_patterns path     7  webhook: malformed payloads are 422s
  3  unattended imports: channel, ids, deal linking             8  key-shaped model ids, base_url host changes
  4  the watched folder: where, what, and what is left alone    9  the webhook is local-only unless allowed
  5  a broken user settings file never takes the app down       minors: unknown deal on upload, secrets.env comments
"""
import inspect
import json
import os
import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from conftest import write_seller
from salescoach import config, repo, sources
from salescoach.sources import base, parsers
from salescoach.sources.adapters import folder as folder_mod
from salescoach.sources.adapters.folder import FolderAdapter
from salescoach.web.app import create_app
from test_sources_support import NAMED, generic_bytes

ORIGIN = {"origin": "http://127.0.0.1:8140"}
HOOK = "/import/webhook"
TWO = [{"speaker": "Me", "text": "hi there"}, {"speaker": "Asha Rao", "text": "hello"}]


@pytest.fixture
def client(db):
    app = create_app(start_worker=False, live_factory=None, hub=None)
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


@pytest.fixture
def secret(db):
    return sources.new_webhook_secret()


@pytest.fixture
def drop(db):
    path = config.DATA_DIR / "inbox" / "drop"
    path.mkdir(parents=True)
    return path


def _old(path, seconds=60):
    os.utime(path, (time.time() - seconds, time.time() - seconds))


def _calls(db):
    return db.execute("SELECT * FROM calls ORDER BY started_at, node_id").fetchall()


def _post(client, secret, payload, **headers):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return client.post(HOOK, headers={"x-salescoach-secret": secret, **headers}, content=body)


# ---- 1. the parser cannot be made to hang ---------------------------------------------------------------

def test_1_label_patterns_are_linear_on_a_whitespace_run():
    text = "Me: hello there\n" + " " * 200_000 + "x\nThem: ok"
    started = time.perf_counter()
    turns = parsers.labelled(text)
    assert time.perf_counter() - started < 1.0                          # was O(n^3): 1.6 KB took 10 s
    assert [t[0] for t in turns] == ["Me", "Them"]
    mixed = "Me: hi" + ("\n" + " " * 40) * 5000 + "Them: ok"           # newlines and spaces, 200 KB
    started = time.perf_counter()
    assert [t[0] for t in parsers.labelled(mixed, strict=True)] == ["Me", "Them"]
    assert time.perf_counter() - started < 1.0


def test_1_label_patterns_still_split_the_same_way():
    text = "  Them: Sir. Good afternoon.  Me: Hello\nAsha Rao: one\nNote: inside a turn stays\nMe:\n\nnext line"
    assert [(l, b) for l, b, _ in parsers.labelled(text)] == [
        ("Them", "Sir. Good afternoon."), ("Me", "Hello"), ("Asha Rao", "one\nNote: inside a turn stays"),
        ("Me", "next line")]
    assert [l for l, _, _ in parsers.labelled(text, strict=True)] == ["Them", "Me", "Me"]


def test_1_caption_tags_are_bounded():
    cue = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n"
    started = time.perf_counter()
    parsers.parse_vtt(cue + "<a" * 100_000 + "\n")                      # was quadratic
    parsers.parse_vtt(cue + "<v" + ".a" * 60 + ">hello\n")            # was exponential
    assert time.perf_counter() - started < 1.0
    ok = parsers.parse_vtt(cue + "<v.loud.fast Asha Rao>We lose two days.\n")
    assert [(t["speaker_label"], t["text"]) for t in ok.turns] == [("Asha Rao", "We lose two days.")]


def test_1_one_size_cap_on_every_door(client, db):
    big = "Me: hi\n" + "x" * (parsers.MAX_INPUT_BYTES + 10)
    with pytest.raises(parsers.UnrecognisedTranscript, match="larger than 5 MB"):
        parsers.labelled(big)
    with pytest.raises(parsers.UnrecognisedTranscript, match="larger than 5 MB"):
        parsers.parse_any(big.encode(), "a.txt")
    from salescoach.sources import paste
    with pytest.raises(ValueError, match="larger than 5 MB"):
        paste.import_text(db, big, "x")
    r = client.post("/import/text", headers=ORIGIN, data={"text": big, "title": "x"})
    assert r.status_code in (303, 400) and _calls(db) == []            # 400: the form parser's own part limit


def test_1_webhook_parses_off_the_event_loop():
    from salescoach.sources import web
    src = inspect.getsource(web.import_webhook)
    assert inspect.iscoroutinefunction(web.import_webhook)
    assert "await run_in_threadpool(_import_webhook_body" in src


def test_1_a_file_is_renamed_while_it_is_read_and_a_leftover_is_set_aside(db, drop, monkeypatch):
    seen = {}
    real = folder_mod.normalize_file

    def spy(kind, data, filename=None, title=None, trusted=True):
        seen["during"] = sorted(p.name for p in drop.iterdir())
        return real(kind, data, filename, title, trusted)

    monkeypatch.setattr(folder_mod, "normalize_file", spy)
    (drop / "acme.txt").write_text(NAMED)
    _old(drop / "acme.txt")
    result = sources.poll(db, kinds=["folder"], force=True)["folder"]
    assert seen["during"] == ["acme.txt.processing"] and len(result["imported"]) == 1
    assert sorted(p.name for p in drop.iterdir()) == ["processed"]

    # a run that died mid-parse: the leftover gets its name back and is remembered, not parsed again
    monkeypatch.setattr(folder_mod, "STALE_PROCESSING_S", 0.0)
    (drop / "poison.txt.processing").write_text("Me: hi\nThem: this one hung last time\n")
    calls = {}

    def never(*a, **k):
        calls["parsed"] = True
        return real(*a, **k)

    monkeypatch.setattr(folder_mod, "normalize_file", never)
    result = sources.poll(db, kinds=["folder"], force=True)["folder"]
    assert "parsed" not in calls and result["listed"] == 0
    assert sorted(p.name for p in drop.iterdir()) == ["poison.txt", "processed"]
    skipped = json.loads(db.execute("SELECT value FROM state WHERE key='sources:folder:skipped'").fetchone()[0])
    assert [e["name"] for e in skipped.values()] == ["poison.txt"] and "did not finish" in next(iter(skipped.values()))["error"]
    # and it stays set aside on the next poll, even under another name
    (drop / "poison.txt").rename(drop / "renamed.txt")
    _old(drop / "renamed.txt")
    assert sources.poll(db, kinds=["folder"], force=True)["folder"]["listed"] == 0


# ---- 3. what an unattended import may claim ------------------------------------------------------------------

def _acme(db):
    acct = repo.create_account(db, "Acme", ["acme.test"])
    deal = repo.create_deal(db, "Acme pilot", account_id=acct)
    db.commit()
    return deal


def test_3_webhook_cannot_declare_seller_speech_or_link_strangers_to_the_deal(client, db, secret):
    deal = _acme(db)
    payload = {"participants": [{"name": "Asha Rao", "email": "asha@acme.test"},
                                {"name": "Mallory", "email": "mallory@evil.test"}],
               "turns": [{"speaker": "Asha Rao", "text": "We need the contract."},
                         {"speaker": "Someone", "channel": "me", "text": "I will send the pricing to Mallory by Friday."}]}
    r = _post(client, secret, payload)
    assert r.status_code == 201, r.text
    call = db.execute("SELECT * FROM calls WHERE node_id=?", (r.json()["call_id"],)).fetchone()
    assert call["deal_id"] == deal
    assert [t["channel"] for t in repo.turns(db, call["node_id"])] == ["them", "them"]      # the payload's channel is ignored
    from salescoach.validators import recipients
    assert set(recipients.allowed_recipients(db, None, deal)) == {"asha@acme.test"}             # not on the deal
    assert "mallory@evil.test" in recipients.allowed_recipients(db, call["node_id"], None)     # on this call only


def test_3_payload_ids_never_take_another_adapters_key(client, db, secret):
    r = _post(client, secret, {"id": "ff-7", "source": "fireflies", "turns": TWO})
    assert r.status_code == 201 and _calls(db)[0]["source_ref"] == "ext:fireflies:ff-7"
    ff = json.dumps({"id": "ff-8", "title": "x", "sentences": [{"speaker_name": "Me", "text": "hi"},
                                                                {"speaker_name": "Asha Rao", "text": "yo"}]}).encode()
    r = _post(client, secret, ff)                                       # a Fireflies-shaped body, same story
    assert r.status_code == 201 and {c["source_ref"] for c in _calls(db)} == {"ext:fireflies:ff-7", "ext:fireflies:ff-8"}
    # the user's own upload of a Fireflies export keeps the native key, so it is one call with the API's
    from salescoach.sources.adapters.upload import UploadAdapter
    assert UploadAdapter().normalize(ff, "export.json").source_ref == "fireflies:ff-8"
    assert FolderAdapter.kind == "folder" and base.from_parsed(parsers.parse_any(ff, "x.json"), "folder",
                                                                trusted=False).source_ref == "ext:fireflies:ff-8"


def test_3_the_folder_and_the_pollers_link_account_addresses_only(db, drop):
    deal = _acme(db)
    payload = {"participants": [{"name": "Asha Rao", "email": "asha@acme.test"}, {"name": "M", "email": "m@evil.test"}],
               "turns": [{"speaker": "Asha Rao", "text": "hi", "channel": "me"}, {"speaker": "Me", "text": "yo"}]}
    (drop / "zap.json").write_text(json.dumps(payload))
    _old(drop / "zap.json")
    call = sources.poll(db, kinds=["folder"], force=True)["folder"]["imported"][0]
    linked = {r[0] for r in db.execute("SELECT p.email FROM deal_people dp JOIN people p ON p.node_id=dp.person_id "
                                       "WHERE dp.deal_id=?", (deal,))}
    assert linked == {"asha@acme.test"}
    assert [t["channel"] for t in repo.turns(db, call)] == ["them", "me"]
    # by hand (the Import page) everyone the seller listed is put on the deal, as before
    from salescoach.sources.adapters.upload import UploadAdapter
    nt = UploadAdapter().normalize(json.dumps({**payload, "title": "by hand"}).encode(), "by-hand.json")
    base.import_normalized(db, nt, deal_id=deal)
    linked = {r[0] for r in db.execute("SELECT p.email FROM deal_people dp JOIN people p ON p.node_id=dp.person_id "
                                       "WHERE dp.deal_id=?", (deal,))}
    assert linked == {"asha@acme.test", "m@evil.test"}


# ---- 4. the watched folder --------------------------------------------------------------------------------------

def test_4_folders_that_cannot_be_watched(db, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(folder_mod.Path, "home", classmethod(lambda cls: home))
    for bad, word in ((str(home), "home folder"), ("/", "whole disk"), (str(home / "Documents"), "Documents itself"),
                      (str(config.DATA_DIR.parent), "data folder"), (str(config.ROOT.parent), "program folder"),
                      ("relative/path", "full path")):
        assert word in (folder_mod.refusal(bad) or ""), bad
    assert folder_mod.refusal(str(home / "Documents" / "Call transcripts")) is None
    with pytest.raises(ValueError, match="Documents itself"):
        sources.save("folder", True, 1, {"path": str(home / "Documents")})
    # a hand-edited sources.yaml is refused at poll time, with the reason on record
    config.save_user("sources", {"sources": [{"kind": "folder", "enabled": True, "options": {"path": str(home)}}]})
    result = sources.poll(db, kinds=["folder"], force=True)["folder"]
    assert "home folder" in result["error"]


def test_4_the_setup_form_says_why(client, db, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(folder_mod.Path, "home", classmethod(lambda cls: home))
    r = client.post("/setup/sources/folder", headers=ORIGIN, data={"enabled": "1", "path": str(home / "Downloads")})
    assert r.status_code == 303 and "Downloads+itself" in r.headers["location"]
    assert "path" not in sources.settings()["folder"]["options"]


def test_4_only_transcripts_leave_the_folder(db, drop):
    (drop / "tax-notes.md").write_text("# taxes\nremember the receipts\n")
    (drop / "LICENSE").write_text("MIT\n")
    (drop / "package.json").write_text('{"name": "my-app", "version": "1.0.0"}')
    (drop / "notes.txt").write_text("nothing labelled here")
    (drop / "acme.txt").write_text(NAMED)
    for p in drop.iterdir():
        _old(p)
    result = sources.poll(db, kinds=["folder"], force=True)["folder"]
    assert result["listed"] == 3 and len(result["imported"]) == 1 and len(result["errors"]) == 2
    assert sorted(p.name for p in drop.iterdir()) == ["LICENSE", "notes.txt", "package.json", "processed", "tax-notes.md"]
    assert not (drop / "failed").exists()
    # the two that were tried are remembered; the next poll reads nothing
    assert sources.poll(db, kinds=["folder"], force=True)["folder"]["listed"] == 0


# ---- 7. the webhook answers 422, never 500, and keeps no orphan ----------------------------------------------

@pytest.mark.parametrize("payload", [
    {"title": ["x"], "turns": TWO},
    {"data": {"transcript": "hello"}},
    {"started_at": "2026-09-01T10:00:00Z", "turns": [{"speaker": "Me", "text": "hi", "start": 1e300}, TWO[1]]},
    b"[" * 200_000 + b"]" * 200_000,
    {"participants": {"a": 1}, "turns": TWO},
    {"turns": [{"speaker": {"x": 1}, "text": "hi"}, {"speaker": "Me", "text": "yo"}]},
    {"turns": TWO, "started_at": [1, 2], "summary": {"x": 1}, "id": {"a": 1}},
    {"turns": "not a list"},
    {"sentences": "nope"},
    {"transcript": {"nope": 1}},
    {"turns": [{"speaker": "Me", "text": {"deep": "dict"}}, TWO[1]], "title": 12},
])
def test_7_malformed_payloads(client, db, secret, payload):
    r = _post(client, secret, payload)
    assert r.status_code in (201, 422), (r.status_code, r.text)
    inbox = config.DATA_DIR / "inbox"
    files = sorted(str(p.relative_to(inbox)) for p in inbox.rglob("*") if p.is_file()) if inbox.exists() else []
    if r.status_code == 422:
        assert files == [] and _calls(db) == []
    else:
        assert len(files) == 1 and len(_calls(db)) == 1


@pytest.mark.sqlite_only          # holds SQLite's one write lock from a raw handle; Postgres has no such lock
def test_7_a_locked_store_is_a_503_not_a_bad_payload(client, db, secret):
    other = sqlite3.connect(db.execute("PRAGMA database_list").fetchone()[2])
    other.execute("PRAGMA busy_timeout = 0")
    other.execute("BEGIN IMMEDIATE")
    try:
        from salescoach.store import stores
        real = stores.sales

        def impatient(path=None):
            conn = real(path)
            conn.execute("PRAGMA busy_timeout = 50")
            return conn

        import salescoach.sources.web as web
        web.stores.sales, saved = impatient, web.stores.sales
        try:
            r = _post(client, secret, {"turns": TWO})
        finally:
            web.stores.sales = saved
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert r.status_code == 503 and "again" in r.json()["error"]


# ---- 9. the webhook is local-only until the user says otherwise --------------------------------------------------

def test_9_local_only_by_default_and_forwarded_requests_are_refused(client, db, secret):
    assert _post(client, secret, {"turns": TWO}).status_code == 201                       # this machine
    for remote in ({"host": "coach.example-tunnel.test"}, {"x-forwarded-for": "203.0.113.9"},
                   {"forwarded": "for=203.0.113.9"}, {"cf-connecting-ip": "203.0.113.9"}):
        r = _post(client, secret, {"turns": TWO}, **remote)
        assert r.status_code == 403, remote
    assert len(_calls(db)) == 1
    # a tunnel that rewrites Host: everything it forwards is refused, the Send button included
    forged = {**ORIGIN, "x-forwarded-for": "203.0.113.9", "host": "127.0.0.1:8140"}
    r = client.post("/import/text", headers=forged, data={"text": NAMED, "title": "x"})
    assert r.status_code == 403 and "tunnel" in r.text
    assert client.get("/", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 403
    assert client.get("/").status_code == 200                                              # a browser here is fine


def test_9_allow_remote_opens_the_webhook_and_nothing_else(client, db, secret):
    sources.save("webhook", True, options={"allow_remote": True})
    tunnel = {"host": "coach.example-tunnel.test", "x-forwarded-for": "203.0.113.9"}
    assert _post(client, secret, {"turns": TWO}, **tunnel).status_code == 201
    assert _post(client, secret[:-1] + "x", {"turns": TWO}, **tunnel).status_code == 403
    assert client.post("/import/text", headers={**ORIGIN, **tunnel}, data={"text": NAMED}).status_code == 403
    assert client.get("/", headers=tunnel).status_code == 403
    # the setup page carries the switch and the warning, and the on/off button does not reset it
    page = client.get("/setup/sources").text
    assert "Allow requests through a tunnel" in page and "not rewrite the Host header" in page and "/import/webhook" in page
    client.post("/setup/sources/webhook", headers=ORIGIN, data={})                           # "Switch off"
    assert sources.settings()["webhook"]["options"]["allow_remote"] is True
    client.post("/setup/sources/webhook", headers=ORIGIN, data={"remote_form": "1"})         # unticked
    assert sources.settings()["webhook"]["options"]["allow_remote"] is False
    assert _post(client, secret, {"turns": TWO}, **tunnel).status_code == 403


# ---- 2. a verdict on the learning page holds on every surface the legacy table feeds ----------------------

TAG2 = "new:pitches_before_discovery"
SAY2 = "Pitches before discovery"


def _learned(db):
    from test_learning_support import calls_with_tag, make_deal
    from salescoach.learning import patterns as learned
    from salescoach.memory import patterns as legacy
    d1, d2 = make_deal(db, "Acme"), make_deal(db, "Globex")
    calls_with_tag(db, TAG2, [(d1, 1, True), (d2, 2, True), (d1, 3, True), (d2, 4, True)])
    legacy.recompute(db)
    learned.recompute(db)
    # a coach report that named the same habit before the verdict
    db.execute("INSERT INTO coach_reports(calls_analysed,json,created_at) VALUES (4,?,?)", (json.dumps(
        {"priority": {"tag": TAG2, "practice": "Ask three questions first"}, "say_differently": ["Say less"]}),
        "2026-09-01T00:00:00+00:00"))
    db.commit()
    return d1, learned.pattern_id("seller", TAG2)


def _surfaces(db, client, deal):
    from salescoach.intel import coach, prep
    from salescoach.memory import patterns as legacy
    brief = prep.render_facts(prep.assemble(db, deal))
    prio = legacy.active_priority(db)
    report = coach.build(db)
    today = client.get("/").text
    return {"prep": SAY2 in brief or "Ask three questions" in brief or "Say less" in brief,
            "today": (prio is not None and prio["tag"] == TAG2) or SAY2 in today,
            "coach": any(p["tag"] == TAG2 for p in report["patterns"]) or report["default_priority"] == TAG2
                     or any(o["tag"] == TAG2 for o in report["observations"])}


@pytest.mark.parametrize("verdict", ["wrong", "retired", "no_prompt"])
def test_2_verdicts_gate_the_prep_brief_the_today_card_and_the_coach_report(db, client, verdict):
    from salescoach.learning import patterns as learned
    from salescoach.memory import patterns as legacy
    deal, pid = _learned(db)
    assert _surfaces(db, client, deal) == {"prep": True, "today": True, "coach": True}      # fed before the verdict
    if verdict == "no_prompt":
        learned.set_prompt_use(db, pid, use=False)
    else:
        learned.set_user_state(db, pid, verdict)
    db.commit()
    legacy.recompute(db)                                                                     # a later recompute changes nothing
    assert _surfaces(db, client, deal) == {"prep": False, "today": False, "coach": False}
    row = db.execute("SELECT status FROM seller_patterns WHERE tag=?", (TAG2,)).fetchone()
    assert row["status"] == ("active" if verdict == "no_prompt" else "retired")            # Wrong/Retire are mirrored
    # the undo brings it back
    if verdict == "no_prompt":
        learned.set_prompt_use(db, pid, use=True)
    else:
        learned.set_user_state(db, pid, None)
    db.commit()
    assert db.execute("SELECT status FROM seller_patterns WHERE tag=?", (TAG2,)).fetchone()["status"] == "active"
    assert _surfaces(db, client, deal) == {"prep": True, "today": True, "coach": True}


# ---- 5. a settings file with a typo is a banner, not a 500 ---------------------------------------------------

@pytest.mark.parametrize("name", ["seller", "models", "sources", "methodology"])
def test_5_a_broken_user_file_falls_back_and_is_named(client, seller_settings, name):
    assert client.get("/setup/you").status_code == 200
    path = seller_settings / f"{name}.yaml"
    path.write_text("name: [unclosed\n  company: : :\n")
    for page in ("/", "/setup", "/setup/you", "/setup/model", "/setup/sources", "/setup/review", "/import", "/learning"):
        r = client.get(page)
        assert r.status_code in (200, 303), (page, r.status_code)
    for page in ("/setup/you", "/"):
        text = client.get(page).text
        if name == "seller" and page == "/":
            continue                                    # no profile: Today redirects to /setup, which carries it
        assert "could not be read" in text and f"{name}.yaml" in text and "line 2" in text, page
    assert [(p["name"], p["line"]) for p in config.user_problems()] == [(f"{name}.yaml", 2)]
    assert config.load_user(name) == {}                 # nothing saved, not a crash
    assert isinstance(config.load(name), dict)          # the tracked defaults
    # saving that page again keeps the unreadable file beside the new one
    config.save_user(name, {"x": 1})
    kept = list(seller_settings.glob(f"{name}.yaml.broken-*"))
    assert len(kept) == 1 and "unclosed" in kept[0].read_text() and config.user_problems() == []


def test_5_an_unreadable_file_and_a_scalar_where_a_list_belongs(client, seller_settings):
    write_seller(seller_settings, {"name": "Priya", "emails": 5, "company": "Acme", "offering": "widgets"})
    assert client.get("/setup/you").status_code == 200        # `emails: 5` is one value, not a TypeError
    from salescoach import seller
    assert seller.emails() == ["5"]
    style = seller_settings / "style.md"
    style.write_text("Own guide\n")
    os.chmod(style, 0)
    try:
        if os.access(style, os.R_OK):
            pytest.skip("running as root: permissions do not apply")
        assert "Own guide" not in config.text("style.md")   # the tracked default, not a PermissionError
    finally:
        os.chmod(style, 0o600)


def test_5_a_broken_tracked_file_still_fails_loudly(tmp_path, monkeypatch):
    tracked = tmp_path / "config"
    tracked.mkdir()
    (tracked / "policy.yaml").write_text("email: [unclosed\n")
    monkeypatch.setattr(config, "CONFIG_DIR", tracked)
    config._load_cached.cache_clear()
    import yaml
    with pytest.raises(yaml.YAMLError):
        config.load("policy")


# ---- 6. migration 4 checks the table it rebuilt, not the whole database ---------------------------------------

def test_6_an_old_orphan_elsewhere_does_not_block_migration_4(tmp_path, monkeypatch):
    from salescoach.store import stores
    from test_sources_migration import V3_CALLS
    monkeypatch.setenv("SALESCOACH_NO_PLUGINS", "1")
    schema = stores.SCHEMA.read_text()
    current = re.search(r"CREATE TABLE IF NOT EXISTS calls \(.*?\n\);", schema, re.S).group(0)
    old = tmp_path / "v3.sql"
    old.write_text(schema.replace(current, V3_CALLS))
    path = tmp_path / "v3.db"
    conn = stores.engine.connect(str(path))
    stores.engine.init(conn, schema=str(old))
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO nodes(id,type,title) VALUES ('call-1','call','c')")
    conn.execute("INSERT INTO calls(node_id,source,title) VALUES ('call-1','granola','c')")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")                # what the sqlite3 CLI does by default
    conn.execute("INSERT INTO turns(call_id,tier,idx,channel,text) VALUES ('call-gone','final',0,'me','x')")
    conn.commit()
    conn.close()
    conn = stores.sales(path)                                # used to raise IntegrityError on every open
    assert conn.execute("PRAGMA user_version").fetchone()[0] == stores.SCHEMA_VERSION
    row = conn.execute("SELECT source, history FROM calls").fetchone()
    assert (row["source"], row["history"]) == ("granola", 1)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


# ---- 8. keys are keys and model ids are model ids ---------------------------------------------------------

KEYISH = "sk-proj-THISISAREALLOOKINGKEY1234567890"


def test_8_a_key_in_the_model_box_is_refused_before_it_goes_anywhere(client, db, seller_settings):
    from salescoach import providers
    form = {"provider": "openai_compatible", "api_key": "", "base_url": "https://gateway.example/v1",
            "heavy_custom": KEYISH, "light_custom": "gpt-5"}
    for path in ("/setup/model/models", "/setup/model/test"):
        r = client.post(path, headers={**ORIGIN, "accept": "application/json"}, data=form)
        assert r.status_code == 200 and not r.json()["ok"] and "not a model id" in r.json()["error"]
        assert KEYISH not in r.text
        page = client.post(path, headers=ORIGIN, data=form).text
        assert KEYISH not in page and "not a model id" in page
    assert db.execute("SELECT value FROM state WHERE key='setup:provider_test'").fetchone() is None
    r = client.post("/setup/model/use", headers=ORIGIN, data=form)
    assert "not+a+model+id" in r.headers["location"] and KEYISH not in r.headers["location"]
    assert not (seller_settings / "models.yaml").exists()
    with pytest.raises(ValueError, match="not a model id"):
        providers.save_choice("openai_compatible", {"base_url": "https://gateway.example/v1"}, {"heavy": KEYISH})
    with pytest.raises(ValueError):
        providers.save_choice("openai_compatible", {}, {"heavy": "m" * 121})
    from salescoach.providers.http_chat import check_base_url
    for bad in ("https://gateway.example/v1?api_key=abc", "https://gateway.example/v1#frag"):
        with pytest.raises(Exception, match="query string or a fragment"):
            check_base_url(bad)


def test_8_a_changed_host_needs_the_key_again(client, db, seller_settings, monkeypatch):
    from salescoach import providers
    sent = []

    def fake_list(provider_key, cfg=None, *, transport=None):
        sent.append(cfg.get("base_url"))
        return ["gpt-5"]

    monkeypatch.setattr(providers, "list_models", fake_list)
    hdr = {**ORIGIN, "accept": "application/json"}
    first = {"provider": "openai_compatible", "api_key": "gw-secret-key-1234", "base_url": "https://gw-a.example/v1"}
    assert client.post("/setup/model/models", headers=hdr, data=first).json()["ok"]
    assert sent == ["https://gw-a.example/v1"] and config.has_secret("LLM_API_KEY")
    # same host, no key typed: fine (a path change is not a host change)
    r = client.post("/setup/model/models", headers=hdr, data={**first, "api_key": "", "base_url": "https://gw-a.example/v2"})
    assert r.json()["ok"] and len(sent) == 2
    # another host, no key typed: refused, nothing sent, the message names both hosts
    r = client.post("/setup/model/models", headers=hdr, data={**first, "api_key": "", "base_url": "https://evil.example/v1"})
    assert not r.json()["ok"] and "gw-a.example" in r.json()["error"] and "evil.example" in r.json()["error"]
    assert len(sent) == 2
    r = client.post("/setup/model/test", headers=hdr, data={**first, "api_key": "", "base_url": "https://evil.example/v1"})
    assert not r.json()["ok"] and "Enter the API key again" in r.json()["error"]
    r = client.post("/setup/model/use", headers=ORIGIN, data={**first, "api_key": "", "base_url": "https://evil.example/v1",
                                                              "heavy_custom": "gpt-5", "light_custom": "gpt-5"})
    assert "Enter+the+API+key+again" in r.headers["location"] and not (seller_settings / "models.yaml").exists()
    # the key typed again for the new host: sent, and the new host is the known one from now on
    r = client.post("/setup/model/models", headers=hdr, data={**first, "base_url": "https://gw-b.example/v1"})
    assert r.json()["ok"] and sent[-1] == "https://gw-b.example/v1"
    r = client.post("/setup/model/models", headers=hdr, data={**first, "api_key": "", "base_url": "https://gw-b.example/v1"})
    assert r.json()["ok"]


# ---- minors ---------------------------------------------------------------------------------------------------

def test_minor_an_unknown_deal_or_person_on_import_is_a_404(client, db):
    text = b"Me: hello there\nAsha Rao: hi, we need this by Friday\n"
    r = client.post("/import/file", headers=ORIGIN, files={"file": ("a.txt", text, "text/plain")},
                    data={"deal_id": "deal-does-not-exist"})
    assert r.status_code == 404
    r = client.post("/import/text", headers=ORIGIN, data={"text": text.decode(), "deal_id": "deal-does-not-exist"})
    assert r.status_code == 404
    r = client.post("/import/text", headers=ORIGIN, data={"text": text.decode(), "participants": ["person-nope"]})
    assert r.status_code == 404
    assert _calls(db) == []
    assert not (config.DATA_DIR / "inbox").exists()          # no raw file for a refused import


def test_minor_set_secret_keeps_comments_and_other_lines(seller_settings):
    path = seller_settings / "secrets.env"
    path.write_text('# my gateway key, rotate in March\nLLM_API_KEY="old"\n\nexport OTHER=abc\nFATHOM_API_KEY="f1"\n')
    config.set_secret("FATHOM_API_KEY", "f2")
    config.set_secret("NEW_ONE", "n1")
    assert path.read_text() == ('# my gateway key, rotate in March\nLLM_API_KEY="old"\n\nexport OTHER=abc\n'
                                'FATHOM_API_KEY="f2"\nNEW_ONE="n1"\n')
    assert config.secret("FATHOM_API_KEY") == "f2" and config.secret("LLM_API_KEY") == "old"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
