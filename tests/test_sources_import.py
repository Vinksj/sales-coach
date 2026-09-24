"""The one import path: who is the seller, the needs_speaker hold, idempotency, the history flag.

The bug this phase fixes, end to end: a recorder labels speakers with real names, the old paste
path only knew the literal "Me", so the seller's turns landed on `them` and the evidence
validator threw the seller's own explicit commitments down to low confidence."""
import json

import pytest

from conftest import SELLER, write_seller
from salescoach import config, repo, users
from salescoach.orchestrator import worker, workflow
from salescoach.sources import base, granola, parsers, paste
from salescoach.sources.adapters.upload import UploadAdapter
from test_core_pipeline import ACTIONS1, _script, _setup
from test_sources_support import NAMED, STRANGERS


def channels(db, call):
    return [(t["channel"], t["speaker_cluster"]) for t in repo.turns(db, call)]


def pending(db, call):
    return db.execute("SELECT COUNT(*) FROM wf_events WHERE entity_id=? AND type='CALL_ENDED'", (call,)).fetchone()[0]


# ---- speaker mapping -------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["Maya Iyer", "maya iyer", "MAYA  IYER", "Maya", "maya", "maya.iyer",
                                   "Me", "Maya-Iyer", "Maya I."])
def test_a_label_that_is_the_seller_goes_to_me(db, seller_settings, label):
    write_seller(seller_settings, {**SELLER, "aliases": ["Maya I"]})
    # through the normalised path, not the text splitter: a recorder's JSON may carry any casing
    nt = base.NormalizedTranscript("webhook", None, "x", turns=[{"speaker_label": label, "text": "I'll send it."},
                                                                 {"speaker_label": "Asha Rao", "text": "Thanks."}])
    call = base.import_normalized(db, nt).call_id
    assert channels(db, call) == [("me", "me"), ("them", "Asha Rao")]


def test_a_participant_with_the_sellers_address_is_the_seller(db):
    nt = base.NormalizedTranscript("webhook", "webhook:1", "x", participants=[
        {"name": "SJ (Tessel)", "email": "maya@tessel.test"}, {"name": "Asha Rao", "email": "asha@acme.test"}],
        turns=[{"speaker_label": "SJ (Tessel)", "text": "hello"}, {"speaker_label": "Asha Rao", "text": "hi"}])
    out = base.import_normalized(db, nt)
    assert not out.needs_speaker and channels(db, out.call_id) == [("me", "me"), ("them", "Asha Rao")]
    # the buyer's turns point at the person the participant list resolved to; the seller is nobody's buyer
    asha = repo.find_person_by_email(db, "asha@acme.test")
    assert [t["person_id"] for t in repo.turns(db, out.call_id)] == [None, asha]
    people = {p["name"]: p["is_me"] for p in repo.call_participants(db, out.call_id)}
    assert people == {"Maya Iyer": 1, "Asha Rao": 0}
    assert db.execute("SELECT COUNT(*) FROM people WHERE email='maya@tessel.test' AND is_me=0").fetchone()[0] == 0


def test_timestamps_and_raw_payload_are_kept(db):
    nt = UploadAdapter().normalize(b"[00:00:05] Maya Iyer: hello\n[00:00:09] Asha Rao: hi\n", "acme call.txt")
    out = base.import_normalized(db, nt)
    assert [t["t_start"] for t in repo.turns(db, out.call_id)] == [5.0, 9.0]
    row = repo.get_call(db, out.call_id)
    assert (row["source"], row["title"], row["wf_state"], row["history"], row["audio_dir"]) == \
        ("upload", "acme call", "diarized", 0, None)
    saved = list((config.DATA_DIR / "inbox" / "upload").iterdir())
    assert len(saved) == 1 and "Asha Rao: hi" in saved[0].read_text()


def test_the_named_transcript_keeps_the_sellers_explicit_commitment(db, fake_llm):
    """End to end on the fake model: same call as test_core_pipeline, but labelled with real names."""
    deal, people = _setup(db)
    _script(fake_llm)
    call = paste.import_text(db, NAMED, "NWP weekly", deal_id=deal, participants=people)
    assert [c for c, _ in channels(db, call)] == ["me", "them", "me", "them", "me", "them", "me"]
    assert worker.drain(db) >= 1
    assert repo.get_call(db, call)["wf_state"] == "awaiting_review", repo.get_call(db, call)["wf_error"]
    loop = db.execute("SELECT confidence, source, owner FROM loops WHERE call_id=? AND description=?",
                      (call, ACTIONS1["actions"][0]["description"])).fetchone()
    assert tuple(loop) == ("explicit", "explicit_commitment", "me")         # KEPT, not dropped to low
    actions = json.loads(db.execute("SELECT json FROM artifacts WHERE call_id=? AND kind='actions'",
                                    (call,)).fetchone()[0])["actions"]
    assert actions[0]["validation_notes"] == []


def test_the_bug_it_fixes_is_real(db, fake_llm):
    """The same transcript with the seller forced onto `them` (what the old label rule did) loses the
    commitment: this is the behaviour the mapping exists to prevent."""
    deal, people = _setup(db)
    _script(fake_llm)
    call = paste.import_text(db, NAMED, "NWP weekly", deal_id=deal, participants=people, me_label=None)
    db.execute("UPDATE turns SET channel='them' WHERE call_id=?", (call,))
    db.commit()
    worker.drain(db)
    loop = db.execute("SELECT confidence FROM loops WHERE call_id=? AND description=?",
                      (call, ACTIONS1["actions"][0]["description"])).fetchone()
    assert loop["confidence"] == "low"


# ---- needs_speaker ---------------------------------------------------------------------------

def test_unknown_speakers_are_held_not_guessed(db, fake_llm):
    out = paste.import_text(db, STRANGERS, "Unknown recorder", result=True)
    assert out.created and out.needs_speaker and out.labels == ["Priya S.", "Dev Anand Rao"]
    call = out.call_id
    assert repo.get_call(db, call)["wf_state"] == "needs_speaker" and pending(db, call) == 0
    assert worker.drain(db) == 0 and fake_llm.calls == []                   # nothing downstream picked it up
    with pytest.raises(workflow.PipelineError):
        workflow.run_pipeline(db, call)
    with pytest.raises(workflow.PipelineError):
        workflow.run_pipeline(db, call, from_step="quality_done")           # not even a re-run from a named step
    assert [q["label"] for q in base.speaker_question(db, call)] == ["Priya S.", "Dev Anand Rao"]
    assert base.speaker_question(db, call)[0]["count"] == 2


def test_resolving_fixes_channels_starts_the_pipeline_and_remembers(db):
    call = paste.import_text(db, STRANGERS, "Unknown recorder")
    before = repo.get_call(db, call)["transcript_sha"]
    with pytest.raises(ValueError):
        base.resolve_speaker(db, call, "Somebody Else")
    assert base.resolve_speaker(db, call, "Priya S.") == 2
    assert channels(db, call) == [("me", "me"), ("them", "Dev Anand Rao"), ("me", "me")]
    row = repo.get_call(db, call)
    assert row["wf_state"] == "diarized" and row["transcript_sha"] != before and pending(db, call) == 1
    assert "Maya Iyer" in {p["name"] for p in repo.call_participants(db, call)}
    with pytest.raises(ValueError):
        base.resolve_speaker(db, call, "Priya S.")                         # answered once
    # the next transcript from that recorder is not held
    again = paste.import_text(db, "Priya S.: Hello again.\nDev Anand Rao: Hello.", "Next call", result=True)
    assert not again.needs_speaker and channels(db, again.call_id)[0] == ("me", "me")
    assert users.remembered_labels(db) == ["Priya S."]                   # remembered for this user


def test_none_of_them_is_an_answer(db):
    call = paste.import_text(db, STRANGERS, "Two buyers talking")
    assert base.resolve_speaker(db, call, base.NOT_PRESENT) == 0
    assert {c for c, _ in channels(db, call)} == {"them"} and repo.get_call(db, call)["wf_state"] == "diarized"
    assert users.remembered_labels(db) == []


def test_generic_speaker_labels_are_asked_but_never_remembered(db):
    call = paste.import_text(db, "Speaker A: I'll send it.\nSpeaker B: Thanks.", "Diarized only")
    assert repo.get_call(db, call)["wf_state"] == "needs_speaker"
    base.resolve_speaker(db, call, "Speaker A")
    assert users.remembered_labels(db) == []                              # Speaker A is someone else next time


def test_explicit_me_label_and_a_wrong_one(db):
    out = paste.import_text(db, STRANGERS, "x", me_label="priya s", result=True)
    assert not out.needs_speaker and channels(db, out.call_id)[0] == ("me", "me")
    with pytest.raises(ValueError) as err:
        paste.import_text(db, "A One: hi\nB Two: hello", "y", me_label="C Three")
    assert "A One, B Two" in str(err.value)


def test_reimport_with_the_answer_resolves_the_held_call(db):
    held = paste.import_text(db, STRANGERS, "x", result=True)
    again = paste.import_text(db, STRANGERS, "x", me_label="Priya S.", result=True)
    assert again.call_id == held.call_id and not again.created and not again.needs_speaker
    assert repo.get_call(db, held.call_id)["wf_state"] == "diarized"


def test_single_label_and_me_them_follow_the_old_behaviour(db):
    one = paste.import_text(db, "Asha Rao: a monologue.\nAsha Rao: still her.", "one voice", result=True)
    assert not one.needs_speaker and {c for c, _ in channels(db, one.call_id)} == {"them"}
    two = paste.import_text(db, "Me: hi\nThem: hello\nAnita: namaste", "classic", result=True)
    assert channels(db, two.call_id) == [("me", "me"), ("them", "them_1"), ("them", "Anita")]


def test_a_transcript_with_no_speaker_names_is_refused(db):
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello there\n\n00:00:03.000 --> 00:00:04.000\nhi\n"
    with pytest.raises(ValueError) as err:
        base.import_normalized(db, UploadAdapter().normalize(vtt.encode(), "x.vtt"))
    assert "who is speaking" in str(err.value)
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


# ---- idempotency -----------------------------------------------------------------------------

def test_repasting_is_idempotent_whatever_the_whitespace_or_title(db):
    first = paste.import_text(db, NAMED, "NWP weekly")
    assert repo.get_call(db, first)["source_ref"].startswith("paste:")
    again = paste.import_text(db, "\n\n" + NAMED.replace("\n", "\r\n\n") + "  \n", "NWP weekly (again)", result=True)
    assert again.call_id == first and not again.created
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1 and pending(db, first) == 1
    other = paste.import_text(db, NAMED + "\nArjun Kumar: One more thing.", "NWP weekly")
    assert other != first


def test_the_same_file_by_upload_is_one_call_and_an_id_wins_over_content(db):
    data = NAMED.encode()
    a = base.import_normalized(db, UploadAdapter().normalize(data, "a.txt"))
    b = base.import_normalized(db, UploadAdapter().normalize(data, "renamed.txt"))
    assert a.created and not b.created and a.call_id == b.call_id
    from test_sources_support import generic_bytes
    g1 = base.import_normalized(db, UploadAdapter().normalize(generic_bytes(), "zap.json"))
    g2 = base.import_normalized(db, UploadAdapter().normalize(generic_bytes(title="edited later"), "zap2.json"))
    assert g1.call_id == g2.call_id and repo.get_call(db, g1.call_id)["source_ref"] == "ext:otter:zap-7781"


# ---- history replaces the name 'granola' -----------------------------------------------------

GRANOLA = {"id": "11111111-2222-3333-4444-555555555555", "title": "Asha <> Maya",
           "date": "Sep 2, 2026 2:00 PM GMT+5:30", "summary": None,
           "participants": [{"name": "Maya", "email": "maya@tessel.test", "org": ""},
                            {"name": "Asha Rao", "email": "asha.rao@acmefreight.test", "org": ""}],
           "transcript": " Them: Good afternoon.  Me: Thanks. Note: I'll send the deck tomorrow.  Them: Okay."}


def test_granola_backfill_is_history_and_a_polled_granola_meeting_is_not(db):
    old = granola.import_meeting(db, GRANOLA["id"], data=dict(GRANOLA))
    row = repo.get_call(db, old)
    assert (row["source"], row["history"]) == ("granola", 1)
    assert channels(db, old) == [("them", "them_1"), ("me", "me"), ("them", "them_1")]   # strict: "Note:" did not split
    new_id = "99999999-2222-3333-4444-555555555555"
    new = granola.import_meeting(db, new_id, data={**GRANOLA, "id": new_id}, history=False)
    assert repo.get_call(db, new)["history"] == 0


def test_granola_with_no_me_label_is_not_held(db):
    """Granola labels by audio path, so 'nobody is Me' means the seller was muted, not 'who are you?'."""
    data = {**GRANOLA, "transcript": " Them: One.  Speaker B: Two.  Them: Three."}
    call = granola.import_meeting(db, GRANOLA["id"], data=data)
    assert repo.get_call(db, call)["wf_state"] == "diarized"


def _email_skip(db, call):
    art = db.execute("SELECT json FROM artifacts WHERE call_id=? AND kind='email' ORDER BY id DESC", (call,)).fetchone()
    return json.loads(art[0]).get("skipped") if art else None


def test_history_not_the_source_name_decides_the_follow_up(db, fake_llm):
    deal, people = _setup(db)
    _script(fake_llm)
    hist = paste.import_text(db, NAMED, "Old Fireflies call", deal_id=deal, participants=people,
                             source="fireflies", history=True)
    live = paste.import_text(db, NAMED + "\nArjun Kumar: Bye.", "New Granola call", deal_id=deal,
                             participants=people, source="granola", history=False)
    worker.drain(db)
    assert "imported history" in _email_skip(db, hist)
    assert db.execute("SELECT COUNT(*) FROM emails WHERE call_id=?", (hist,)).fetchone()[0] == 0
    assert _email_skip(db, live) is None
    assert db.execute("SELECT COUNT(*) FROM emails WHERE call_id=?", (live,)).fetchone()[0] == 1


def test_text_ness_is_no_audio_dir_not_a_source_list(db, monkeypatch):
    seen = []
    from salescoach.speech import final
    monkeypatch.setattr(final, "transcribe_call", lambda conn, cid: seen.append(cid))
    text_call = repo.create_call(db, source="some_new_recorder", title="t", wf_state="diarized")
    audio_call = repo.create_call(db, source="some_new_recorder", title="a", wf_state="captured", audio_dir="/tmp/x")
    workflow.step_transcribe(db, text_call)
    workflow.step_transcribe(db, audio_call)
    assert seen == [audio_call] and not hasattr(workflow, "TEXT_SOURCES")


def test_jarvis_claims_by_history_flag(db, tmp_path, monkeypatch):
    import sqlite3
    from salescoach.integrations import jarvis_bridge
    from salescoach.store import stores
    world = tmp_path / "world.db"
    w = sqlite3.connect(world)
    w.execute("CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    w.commit()
    w.close()
    monkeypatch.setattr(stores, "WORLD_DB", world)
    asha = repo.create_person(db, "Asha Rao", email="asha@acme.test")
    ids = {}
    for name, source, history in (("new", "granola", False), ("old", "fireflies", True)):
        ids[name] = repo.create_call(db, source=source, title=name, wf_state="awaiting_review", history=history,
                                     started_at=stores.now())
        repo.add_participant(db, ids[name], asha)
    db.commit()
    assert jarvis_bridge.claim_calls(db) == 1
    claimed = json.loads(sqlite3.connect(world).execute("SELECT value FROM state WHERE key='sales:calls'").fetchone()[0])
    assert [c["call_id"] for c in claimed] == [ids["new"]]


def test_parsed_channel_is_respected():
    nt = base.NormalizedTranscript("webhook", None, turns=[{"speaker_label": "Mic", "text": "a", "channel": "me"},
                                                           {"speaker_label": "Room", "text": "b", "channel": "them"}])
    mapped, hold = base.map_speakers(nt)
    assert [(c, k) for c, k, _ in mapped] == [("me", "me"), ("them", "Room")] and not hold
    assert parsers.generic_to_parsed({"turns": [{"speaker": "x", "text": "y", "channel": "me"}]}).turns[0]["channel"] == "me"
