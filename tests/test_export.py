"""Per-user export (Phase 8), on the local install: /me/export streams a zip of the user's own rows with a
README; `salescoach export --user` writes the same. Isolation (a rep's export holds nobody else's rows, a
manager's holds their own work only) is in tests/isolation/test_export_rls.py."""
import io
import json
import zipfile

from salescoach import cli
from test_web import app, client, gmail, live, processed  # noqa: F401


def _zip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: json.loads(zf.read(name)) for name in zf.namelist()}


def test_me_export_streams_the_users_own_data(client, processed):  # noqa: F811
    r = client.get("/me/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    files = _zip(r.content)
    readme = files["README.json"]
    assert readme["files"]["calls.json"]["rows"] == 1
    assert [c["node_id"] for c in files["calls.json"]] == [processed["call"]]
    assert len(files["turns.json"]) > 0 and len(files["loops.json"]) == len(processed["loops"])
    assert files["users.json"][0]["id"] == "local"
    for name, meta in readme["files"].items():                       # every file is described, and present
        assert name in files and len(files[name]) == meta["rows"]
    assert all("secret_enc" not in row for row in files.get("source_connections.json", []))
    assert all("id" not in row for row in files.get("sessions.json", []))


def test_the_export_command(processed, tmp_path, capsys):  # noqa: F811
    out = tmp_path / "me.zip"
    assert cli.main(["export", "--user", "local", "--out", str(out)]) == 0
    assert "own data" in capsys.readouterr().out
    files = _zip(out.read_bytes())
    assert [c["node_id"] for c in files["calls.json"]] == [processed["call"]]
