"""/import/audio under two reps (Postgres, cloud mode): the security review's findings A, B and C.

  A  An uploaded HLS playlist (.m3u8) naming ../calls/<A's call>/them.flac made ffmpeg read A's recorded audio
     and import it as B's call. Only sniffed single-file recordings reach ffmpeg now, with -f and
     -protocol_whitelist file; the form takes only recording suffixes.
  B  No size cap and the saved upload was never deleted: a cap (413 / a message) and the inbox file is gone
     after every import, refused or not.
  C  The dedupe key audio_file:<sha> named nobody: B's import of the same bytes as A hit A's UNIQUE source_ref
     (a 500-ish error that told B a colleague holds that recording). The key is the importer's own now.
"""
import io

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from salescoach import config
from salescoach.web import app as app_module

from test_route_crawl import A, B, ORIGIN, cloud, two_reps  # noqa: F401

pytestmark = pytest.mark.postgres_only


def _wav(seconds=2.0) -> bytes:
    rate = 16000
    t = np.arange(int(rate * seconds)) / rate
    data = np.stack([0.3 * np.sin(2 * np.pi * 300 * t), 0.3 * np.sin(2 * np.pi * 700 * t)], axis=1).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, data, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@pytest.fixture
def client(db, two_reps, cloud):
    return TestClient(app_module.create_app(start_worker=False, live_factory=None, hub=None), follow_redirects=False)


def upload(client, who, name, body, layout="stereo_me_left"):
    return client.post("/import/audio", files={"file": (name, body, "application/octet-stream")},
                       data={"title": name, "layout": layout}, headers={"x-test-user": who, "accept": "text/html", **ORIGIN})


def _inbox():
    folder = config.DATA_DIR / "inbox"
    return sorted(p.name for p in folder.glob("upload-*")) if folder.exists() else []


def test_a_playlist_of_another_reps_audio_imports_nothing(client, pg_owner):
    r = upload(client, A, "customer.wav", _wav())
    assert r.status_code == 303 and r.headers["location"].startswith("/calls/"), r.headers
    a_call = r.headers["location"].split("?")[0].rsplit("/", 1)[-1]
    playlist = f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:2,\n../calls/{a_call}/them.flac\n#EXT-X-ENDLIST\n".encode()
    for name in ("notes.m3u8", "notes.wav", "notes.mp3"):          # the suffix is refused, or the content is
        r = upload(client, B, name, playlist, layout="mono_them")
        assert r.status_code == 303 and r.headers["location"].startswith("/import?err="), (name, r.headers)
    assert pg_owner.execute("SELECT COUNT(*) FROM calls WHERE owner_id=?", (B,)).fetchone()[0] == 0
    assert _inbox() == []


def test_the_same_recording_imports_for_two_reps_and_dedupes_per_rep(client, pg_owner):
    body = _wav(1.0)
    first, second = upload(client, A, "board-call.wav", body), upload(client, B, "board-call.wav", body)
    for r in (first, second):
        assert r.status_code == 303 and r.headers["location"].startswith("/calls/"), r.headers
    again = upload(client, B, "board-call.wav", body)                     # B's own duplicate: B's call again
    assert again.headers["location"].split("?")[0] == second.headers["location"].split("?")[0]
    owners = [r[0] for r in pg_owner.execute("SELECT owner_id FROM calls ORDER BY owner_id")]
    assert owners == [A, B]
    assert _inbox() == []                                                 # no upload is kept


def test_an_upload_over_the_cap_is_refused(client, pg_owner, monkeypatch):
    monkeypatch.setenv("SALESCOACH_MAX_AUDIO_MB", "0.05")                # ~52 KB
    r = upload(client, A, "long.wav", _wav(3.0))                           # ~190 KB
    assert r.status_code == 413, r.status_code
    assert pg_owner.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0 and _inbox() == []
    monkeypatch.setenv("SALESCOACH_MAX_IMPORT_MB", "0.001")
    r = client.post("/import/text", data={"text": "Me: hi\n" * 400, "title": "x"},
                    headers={"x-test-user": A, "accept": "text/html", **ORIGIN})
    assert r.status_code == 413
