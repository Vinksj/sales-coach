import numpy as np
import pytest
import soundfile as sf

from salescoach import config, repo
from salescoach.speech import diarize
from salescoach.speech.diarize import assign_clusters, diarize_call, find_models, label_clusters
from salescoach.store import stores

TURNS = [("me", 0.0, 1.0), ("them", 1.0, 3.0), ("them", 3.5, 5.0), ("me", 5.0, 6.0), ("them", 6.5, 8.0)]


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("SALES_DB", str(tmp_path / "sales.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    conn = stores.sales()
    audio_dir = config.calls_dir() / "c1"
    audio_dir.mkdir()
    call_id = repo.create_call(conn, source="capture", title="t", audio_dir=str(audio_dir),
                               wf_state="final_transcribed")
    for idx, (channel, t0, t1) in enumerate(TURNS):
        conn.execute("INSERT INTO turns(call_id,tier,idx,channel,t_start,t_end,text) VALUES (?,?,?,?,?,?,?)",
                     (call_id, "final", idx, channel, t0, t1, f"turn {idx}"))
    conn.commit()
    yield conn, call_id, audio_dir
    conn.close()


def _clusters(conn, call_id):
    return [r["speaker_cluster"] for r in repo.turns(conn, call_id, "final")]


def _speakers(conn, call_id):
    return sorted((r["cluster"], r["channel"]) for r in conn.execute(
        "SELECT cluster, channel FROM speakers WHERE call_id=?", (call_id,)))


def test_skip_without_models(call):
    conn, call_id, _ = call
    result = diarize_call(conn, call_id)                        # default dir under DATA_DIR is empty
    assert result["skipped"] == "diarization models not present" and result["clusters"] == ["them_1"]
    assert _clusters(conn, call_id) == ["me", "them_1", "them_1", "me", "them_1"]
    assert _speakers(conn, call_id) == [("me", "me"), ("them_1", "them")]
    assert repo.get_call(conn, call_id)["wf_state"] == "diarized"


def test_with_models_assigns_by_overlap(call, monkeypatch):
    conn, call_id, audio_dir = call
    models = config.DATA_DIR / "models" / "diarization"
    models.mkdir(parents=True)
    (models / "segmentation.onnx").write_bytes(b"")
    (models / "nemo_en_titanet_small.onnx").write_bytes(b"")
    sf.write(audio_dir / "them.flac", np.zeros(8 * 16000, np.float32), 16000, subtype="PCM_16")
    seen = {}

    def fake_run(audio, rate, seg, emb, **kwargs):
        seen.update(samples=len(audio), seg=seg.name, emb=emb.name)
        return [(0.9, 3.1, 4), (3.4, 5.2, 2), (6.4, 8.0, 4)]

    monkeypatch.setattr(diarize, "run_diarization", fake_run)
    conn.execute("INSERT INTO speakers(call_id,cluster,channel,person_id) VALUES (?,?,?,?)",
                 (call_id, "them_1", "them", "person-kept"))
    result = diarize_call(conn, call_id)
    assert seen == {"samples": 8 * 16000, "seg": "segmentation.onnx", "emb": "nemo_en_titanet_small.onnx"}
    assert result == {"segments": 3, "clusters": ["them_1", "them_2"], "turns_assigned": 3}
    assert _clusters(conn, call_id) == ["me", "them_1", "them_2", "me", "them_1"]
    assert _speakers(conn, call_id) == [("me", "me"), ("them_1", "them"), ("them_2", "them")]
    kept = conn.execute("SELECT person_id FROM speakers WHERE call_id=? AND cluster='them_1'", (call_id,)).fetchone()
    assert kept["person_id"] == "person-kept"                  # a mapped person survives a re-run


def test_assign_and_label():
    segments = label_clusters([(2.8, 5.2, 7), (0.0, 2.5, 3), (5.9, 8.0, 3)])
    assert segments == [(2.8, 5.2, "them_2"), (0.0, 2.5, "them_1"), (5.9, 8.0, "them_1")]
    turns = [{"t_start": 0.0, "t_end": 2.0}, {"t_start": 3.0, "t_end": 5.0}, {"t_start": 6.0, "t_end": 8.0},
             {"t_start": 9.0, "t_end": 9.5}]
    assert assign_clusters(turns, segments) == ["them_1", "them_2", "them_1", "them_1"]
    assert assign_clusters(turns[:1], []) == ["them_1"]


def test_find_models(tmp_path):
    assert find_models(tmp_path) is None
    (tmp_path / "sherpa-onnx-pyannote-segmentation-3-0").mkdir()
    (tmp_path / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx").write_bytes(b"")
    assert find_models(tmp_path) is None                        # still no embedding model
    (tmp_path / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx").write_bytes(b"")
    seg, emb = find_models(tmp_path)
    assert seg.name == "model.onnx" and "eres2net" in emb.name
