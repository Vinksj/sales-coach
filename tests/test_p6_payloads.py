"""Phase 6: raw payloads. Locally a file under data/inbox as before; in cloud mode a raw_payloads row
(OWNED, in the import's transaction) and no file. `salescoach payloads export` writes them back out."""
import json

import pytest

from salescoach import cli, config, identity, users
from salescoach.sources import base
from salescoach.sources.adapters.upload import UploadAdapter

TEXT = b"[00:00:05] Asha Rao: hello there\n[00:00:09] Bala K: hi, send the deck\n"


def test_local_mode_keeps_the_file_and_writes_no_row(db):
    nt = UploadAdapter().normalize(TEXT, "acme call.txt")
    out = base.import_normalized(db, nt, me_label="Asha Rao")
    assert out.created
    assert len(list((config.DATA_DIR / "inbox" / "upload").iterdir())) == 1
    assert db.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 0


def test_encodings_round_trip():
    for raw in (b"\x00\x01bytes", "plain text", {"a": [1, 2]}):
        enc, body = base.encode_raw(raw)
        assert base.decode_raw(enc, body) == raw
    assert base.encode_raw(b"x")[0] == "base64" and base.encode_raw("x")[0] == "text" and base.encode_raw({})[0] == "json"


@pytest.fixture
def cloud_user(monkeypatch, db, dialect):
    if dialect != "postgres":
        pytest.skip("cloud mode is Postgres-only")
    monkeypatch.setenv(identity.MODE_ENV, "cloud")
    config._org_cache.clear()
    with identity.activate(identity.LOCAL_ACTOR):              # an admin saves Settings (only an admin may)
        config.save_user("seller", {"company": "Acme", "offering": "Widgets", "own_domains": ["acme.test"]})
    asha = users.create(db, "asha@acme.test", "Asha Rao", timezone="Asia/Kolkata")
    db.commit()
    yield asha["id"]
    config._org_cache.clear()


@pytest.mark.postgres_only
def test_cloud_mode_writes_a_row_in_the_import_transaction_and_no_file(db, cloud_user):
    with identity.session(cloud_user, mode=identity.SERVICE) as conn:
        nt = UploadAdapter().normalize(TEXT, "acme call.txt")
        out = base.import_normalized(conn, nt, me_label="Asha Rao")
        assert out.created
        row = conn.execute("SELECT * FROM raw_payloads").fetchone()
        assert row["owner_id"] == cloud_user and row["source_kind"] == "upload" and row["encoding"] == "text"
        assert "send the deck" in row["body"] and len(row["sha256"]) == 64 and row["source_ref"] == nt.source_ref
        assert not (config.DATA_DIR / "inbox").exists()
        # the same payload again (a second upload of one file) is one row, not two
        again = UploadAdapter().normalize(TEXT, "acme call.txt")
        base.import_normalized(conn, again, me_label="Asha Rao")
        assert conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 1
        # a refused import takes the row back with the rest of the transaction
        other = UploadAdapter().normalize(TEXT.replace(b"deck", b"invoice"), "other.txt")
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("refused"))
        import salescoach.sources.base as mod
        saved = mod._create
        mod._create = boom
        try:
            with pytest.raises(RuntimeError):
                base.import_normalized(conn, other, me_label="Asha Rao")
        finally:
            mod._create = saved
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 1


@pytest.mark.postgres_only
def test_export_cli_writes_files_and_json_lines(db, cloud_user, tmp_path, capsys):
    with identity.session(cloud_user, mode=identity.SERVICE) as conn:
        for name, raw in (("a.txt", TEXT), ("b.json", {"turns": [{"speaker": "Asha Rao", "text": "json payload"}]})):
            nt = base.NormalizedTranscript(source_kind="webhook", source_ref=f"webhook:{name}", title=name,
                                           turns=[{"speaker_label": "Asha Rao", "text": "hello"}, {"speaker_label": "Bala", "text": "hi"}],
                                           raw=raw)
            base.import_normalized(conn, nt, me_label="Asha Rao")
        conn.commit()
    out = tmp_path / "export"
    assert cli.main(["payloads", "export", "--out", str(out), "--as", cloud_user]) == 2
    files = sorted(p.name for p in (out / "webhook").iterdir())
    assert len(files) == 2 and any(f.endswith(".bin") for f in files) and any(f.endswith(".json") for f in files)
    assert cli.main(["payloads", "export", "--as", cloud_user, "--since", "2000-01-01"]) == 2
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert {l["encoding"] for l in lines} == {"base64", "json"} and all(l["owner_id"] == cloud_user for l in lines)
    assert cli.main(["payloads", "export", "--as", cloud_user, "--since", "2999-01-01"]) == 0


def test_nothing_in_the_pipeline_reads_payloads_back():
    """The table and the inbox folder are write-only for the app (support reads them)."""
    import subprocess
    out = subprocess.run(["grep", "-rn", "--include=*.py", "FROM raw_payloads", str(config.ROOT / "salescoach")], capture_output=True, text=True).stdout
    assert all("sources/base.py" in line for line in out.strip().splitlines())
