"""Upload, the webhook and its narrow guard exemption, the speaker question, and the CLI."""
import json

import pytest
from fastapi.testclient import TestClient

from salescoach import cli, config, repo, sources
from salescoach.sources.adapters import MAX_BYTES
from salescoach.web.app import create_app
from test_sources_support import NAMED, STRANGERS, generic_bytes

ORIGIN = {"origin": "http://127.0.0.1:8140"}
HOOK = "/import/webhook"


@pytest.fixture
def client(db):
    app = create_app(start_worker=False, live_factory=None, hub=None)
    return TestClient(app, follow_redirects=False)          # no lifespan: no worker, no scheduler


@pytest.fixture
def secret(db):
    return sources.new_webhook_secret()


def _calls(db):
    return db.execute("SELECT * FROM calls ORDER BY started_at, node_id").fetchall()


# ---- upload ------------------------------------------------------------------------------------------

def test_upload_any_format_and_again(client, db):
    r = client.post("/import/file", headers=ORIGIN, files={"file": ("acme weekly.txt", NAMED.encode(), "text/plain")},
                    data={"title": "", "lang_mode": "auto"})
    assert r.status_code == 303 and "Imported" in r.headers["location"]
    call = _calls(db)[0]
    assert (call["source"], call["title"], call["wf_state"], call["history"]) == ("upload", "acme weekly", "diarized", 0)
    assert [t["channel"] for t in repo.turns(db, call["node_id"])][:2] == ["me", "them"]
    r = client.post("/import/file", headers=ORIGIN, files={"file": ("same.txt", NAMED.encode(), "text/plain")})
    assert "already+imported" in r.headers["location"] and len(_calls(db)) == 1
    r = client.post("/import/file", headers=ORIGIN, files={"file": ("zap.json", generic_bytes(), "application/json")})
    assert r.status_code == 303 and len(_calls(db)) == 2
    assert client.get("/import").status_code == 200 and 'action="/import/file"' in client.get("/import").text


def test_upload_errors_are_shown_not_raised(client, db):
    for name, data, needle in (("notes.txt", b"nothing labelled here", "no+speaker+turns"),
                               ("call.wav", b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 50, "not+text"),
                               ("big.txt", b"A B: " + b"x" * (MAX_BYTES + 10), "larger+than")):
        r = client.post("/import/file", headers=ORIGIN, files={"file": (name, data, "application/octet-stream")})
        assert r.status_code == 303 and r.headers["location"].startswith("/import?err=") and needle in r.headers["location"]
    assert _calls(db) == []
    assert client.post("/import/file", files={"file": ("a.txt", NAMED.encode())}).status_code == 403     # no origin


# ---- which speaker are you? ------------------------------------------------------------------------

def test_the_speaker_question_on_the_call_page(client, db):
    r = client.post("/import/file", headers=ORIGIN, files={"file": ("unknown.txt", STRANGERS.encode(), "text/plain")})
    assert "which+speaker+are+you" in r.headers["location"] and r.headers["location"].endswith("#speaker")
    call = _calls(db)[0]["node_id"]
    page = client.get(f"/calls/{call}").text
    assert "Which speaker are you?" in page and 'value="Priya S."' in page and 'value="Dev Anand Rao"' in page
    assert "Needs you: which speaker are you?" in page                       # the state's label, not "needs_speaker"
    assert "Which speaker are you?" in client.get("/").text                   # and it is on Today, not lost
    assert db.execute("SELECT COUNT(*) FROM wf_events").fetchone()[0] == 0
    # nothing can push a held call into the pipeline
    for path, data in ((f"/calls/{call}/retry", {}), (f"/calls/{call}/rerun", {"from_step": "quality_done"})):
        r = client.post(path, headers=ORIGIN, data=data)
        assert "Say+which+speaker" in r.headers["location"]
    assert db.execute("SELECT COUNT(*) FROM wf_events").fetchone()[0] == 0
    assert client.post(f"/calls/{call}/speaker", data={"label": "Priya S."}).status_code == 403    # guarded like any POST
    r = client.post(f"/calls/{call}/speaker", headers=ORIGIN, data={"label": "Nobody"})
    assert "err=" in r.headers["location"]
    r = client.post(f"/calls/{call}/speaker", headers=ORIGIN, data={"label": "Priya S."})
    assert "2+turns+are+yours" in r.headers["location"]
    assert [t["channel"] for t in repo.turns(db, call)] == ["me", "them", "me"]
    assert repo.get_call(db, call)["wf_state"] == "diarized"
    assert db.execute("SELECT COUNT(*) FROM wf_events WHERE type='CALL_ENDED' AND entity_id=?", (call,)).fetchone()[0] == 1
    assert "Which speaker are you?" not in client.get(f"/calls/{call}").text
    assert client.post("/calls/call-nope/speaker", headers=ORIGIN, data={"label": "x"}).status_code == 404


def test_paste_box_also_asks(client, db):
    r = client.post("/import/text", headers=ORIGIN, data={"text": STRANGERS, "title": "Pasted"})
    assert "which+speaker+are+you" in r.headers["location"]
    r = client.post("/import/text", headers=ORIGIN, data={"text": STRANGERS, "title": "Pasted again"})
    assert len(_calls(db)) == 1


# ---- webhook ---------------------------------------------------------------------------------------

def test_webhook_refuses_when_no_secret_is_configured(client, db):
    r = client.post(HOOK, content=generic_bytes(), headers={"x-salescoach-secret": "guess"})
    assert r.status_code == 403
    # even from the app's own origin there is no unauthenticated mode
    r = client.post(HOOK, content=generic_bytes(), headers=ORIGIN)
    assert r.status_code == 403 and "off" in r.json()["error"]
    assert _calls(db) == []


def test_webhook_wrong_secret_is_403_with_or_without_an_origin(client, db, secret):
    for headers in ({"x-salescoach-secret": "wrong"}, {"x-salescoach-secret": secret[:-1]}, {},
                    {**ORIGIN, "x-salescoach-secret": "wrong"}, ORIGIN):
        assert client.post(HOOK, content=generic_bytes(), headers=headers).status_code == 403, headers
    assert _calls(db) == []


def test_webhook_imports_with_the_right_secret_and_no_origin(client, db, secret):
    acct = repo.create_account(db, "Acme Freight", ["acmefreight.test"])
    deal = repo.create_deal(db, "Acme pilot", account_id=acct)
    db.commit()
    payload = json.loads(generic_bytes())
    payload.update({"deal_id": "deal-evil", "history": True, "wf_state": "done"})       # ignored: it only imports
    r = client.post(HOOK, content=json.dumps(payload).encode(), headers={"X-Salescoach-Secret": secret})
    assert r.status_code == 201 and r.json()["created"] and not r.json()["needs_speaker"]
    call = repo.get_call(db, r.json()["call_id"])
    assert (call["source"], call["source_ref"], call["deal_id"], call["history"], call["wf_state"]) == \
        ("webhook", "ext:otter:zap-7781", deal, 0, "diarized")
    assert [t["channel"] for t in repo.turns(db, call["node_id"])] == ["them", "me", "them"]
    again = client.post(HOOK, content=generic_bytes(), headers={"X-Salescoach-Secret": secret})
    assert again.status_code == 200 and again.json() == {"call_id": call["node_id"], "created": False,
                                                          "needs_speaker": False}
    assert len(_calls(db)) == 1
    saved = list((config.DATA_DIR / "inbox" / "webhook").iterdir())
    assert len(saved) == 1 and json.loads(saved[0].read_text())["id"] == "zap-7781"


def test_webhook_without_an_id_dedupes_on_the_body(client, db, secret):
    body = json.dumps({"title": "No id", "turns": [{"speaker": "Maya Iyer", "text": "hello"},
                                                   {"speaker": "Asha Rao", "text": "hi"}]}).encode()
    first = client.post(HOOK, content=body, headers={"x-salescoach-secret": secret})
    second = client.post(HOOK, content=body, headers={"x-salescoach-secret": secret})
    assert (first.status_code, second.status_code) == (201, 200) and len(_calls(db)) == 1
    assert _calls(db)[0]["source_ref"].startswith("webhook:")


def test_webhook_oversized_and_malformed(client, db, secret):
    auth = {"x-salescoach-secret": secret}
    assert client.post(HOOK, content=b"x" * (MAX_BYTES + 1), headers=auth).status_code == 413

    def chunks():                                        # no Content-Length to trust: the stream itself is capped
        for _ in range(6):
            yield b"y" * (1024 * 1024)
    assert client.post(HOOK, content=chunks(), headers=auth).status_code == 413
    assert client.post(HOOK, content=b"not json", headers=auth).status_code == 422
    assert client.post(HOOK, content=b'{"hello": 1}', headers=auth).status_code == 422
    assert client.post(HOOK, content=b'{"turns": [{"speaker": "", "text": "who?"}]}', headers=auth).status_code == 422
    assert _calls(db) == []


def test_webhook_can_be_switched_off(client, db, secret):
    sources.save("webhook", False)
    assert client.post(HOOK, content=generic_bytes(), headers={"x-salescoach-secret": secret}).status_code == 403
    assert _calls(db) == []


def test_the_secret_opens_the_webhook_and_nothing_else(client, db, secret):
    auth = {"x-salescoach-secret": secret}
    # a browser-style POST without the secret is still stopped by the origin guard, on the webhook too
    for headers in ({"origin": "http://evil.test"}, {"referer": "https://evil.test/x"}, {"origin": "null"}, {}):
        r = client.post(HOOK, content=generic_bytes(), headers=headers)
        assert r.status_code == 403 and "did not come from the sales coach" in r.text, headers
    # the secret does not exempt any other route, method or look-alike path
    for path in ("/import/text", "/import/file", "/import/webhook/", "/import/webhook/x", "/calls/x/speaker", "/deals"):
        assert client.post(path, headers=auth, data={"text": NAMED, "title": "x"}).status_code in (403, 404, 405), path
        assert "did not come from" in client.post(path, headers=auth, data={"text": NAMED}).text or path.startswith(HOOK)
    assert client.put(HOOK, content=generic_bytes(), headers=auth).status_code == 403
    assert _calls(db) == []
    # with the secret, a tunnel's Host header is accepted for the webhook and for nothing else
    tunnel = {**auth, "host": "coach.example-tunnel.test"}
    assert client.get("/", headers={"host": "coach.example-tunnel.test"}).status_code == 403
    assert client.post("/import/text", headers=tunnel, data={"text": NAMED}).status_code == 403
    assert client.post(HOOK, content=generic_bytes(), headers={"host": "coach.example-tunnel.test",
                                                               "x-salescoach-secret": "wrong"}).status_code == 403
    # Review 3: and only once the user switched "Allow requests through a tunnel" on. Off (the default),
    # the webhook answers this machine only, right secret or not.
    assert client.post(HOOK, content=generic_bytes(), headers=tunnel).status_code == 403
    assert _calls(db) == []
    sources.save("webhook", True, options={"allow_remote": True})
    assert client.get("/", headers={"host": "coach.example-tunnel.test"}).status_code == 403
    assert client.post("/import/text", headers=tunnel, data={"text": NAMED}).status_code == 403
    assert client.post(HOOK, content=generic_bytes(), headers=tunnel).status_code == 201


def test_webhook_before_setup_is_refused(client, db, secret, seller_settings):
    from conftest import write_seller
    write_seller(seller_settings, {})
    r = client.post(HOOK, content=generic_bytes(), headers={"x-salescoach-secret": secret})
    assert r.status_code == 409 and _calls(db) == []


# ---- CLI ---------------------------------------------------------------------------------------------

def test_cli_import_file(db, tmp_path, capsys):
    f = tmp_path / "unknown call.txt"
    f.write_text(STRANGERS)
    assert cli.main(["import-file", str(f)]) is None
    out = capsys.readouterr()
    call = out.out.strip()
    assert repo.get_call(db, call)["wf_state"] == "needs_speaker" and "Priya S., Dev Anand Rao" in out.err
    assert cli.main(["import-file", str(f), "--me", "Priya S."]) is None        # the answer, from the CLI
    assert capsys.readouterr().out.strip() == call and repo.get_call(db, call)["wf_state"] == "diarized"

    g = tmp_path / "old.txt"
    g.write_text(NAMED)
    assert cli.main(["import-file", str(g), "--title", "Old one", "--history"]) is None
    row = repo.get_call(db, capsys.readouterr().out.strip())
    assert (row["title"], row["history"], row["source"]) == ("Old one", 1, "upload")
    h = tmp_path / "buyers only.txt"
    h.write_text("A One: hi\nB Two: hello")
    assert cli.main(["import-file", str(h), "--me", "none"]) is None
    assert repo.get_call(db, capsys.readouterr().out.strip())["wf_state"] == "diarized"
    bad = tmp_path / "notes.txt"
    bad.write_text("no labels at all")
    assert cli.main(["import-file", str(bad)]) == 1 and "no speaker turns" in capsys.readouterr().err


def test_cli_sources_list_and_poll(db, capsys, monkeypatch):
    from salescoach import providers
    monkeypatch.setattr(providers, "claude_cli_available", lambda: False)
    assert cli.main(["sources", "list"]) is None
    out = capsys.readouterr().out
    assert "fireflies  off poll  needs FIREFLIES_API_KEY every 15 min  [untested against the live API]" in out
    assert "folder     on  poll  ready every 1 min" in out and "otter      export" in out
    drop = config.DATA_DIR / "inbox" / "drop"
    drop.mkdir(parents=True)
    assert cli.main(["sources", "poll", "folder"]) is None
    assert json.loads(capsys.readouterr().out)["folder"]["listed"] == 0
    assert cli.main(["sources", "webhook-secret"]) is None
    shown = capsys.readouterr().out.splitlines()[0]
    from salescoach.sources.adapters import webhook
    assert webhook.authorised(shown)
