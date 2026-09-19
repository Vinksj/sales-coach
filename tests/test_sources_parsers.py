"""Every export format, on invented but realistically awkward fixtures."""
import json

import pytest

from salescoach.sources import parsers, paste
from test_sources_support import FATHOM_MEETING, FATHOM_TRANSCRIPT, FIREFLIES, GENERIC, NAMED, OTTER, SRT, VTT


def labels(parsed):
    return [t["speaker_label"] for t in parsed.turns]


def test_plain_names_three_words_initials_punctuation_and_non_latin():
    text = ("Date: 3 Sep 2026\nAttendees: A, B\n"
            "Dr. A. K. Sharma: We need this by site.\n"
            "Mary-Jane O'Neil: Agreed. Note: finance signs off first.\n"
            "Ludwig van der Berg: One question. Timeline: six weeks?\n"
            "अनीता शर्मा: ठीक है, शुक्रवार तक भेज दीजिए।\n"
            "王伟: 好的。\n"
            "J.R. Rao (Acme Freight): Fine.\n"
            "[00:12:05] Siobhán Ní Bhriain: Late, sorry.\n"
            "Speaker 2: Who is this?\n")
    parsed = parsers.parse_any(text, "call.txt")
    assert parsed.format == "plain"
    assert labels(parsed) == ["Dr. A. K. Sharma", "Mary-Jane O'Neil", "Ludwig van der Berg", "अनीता शर्मा", "王伟",
                              "J.R. Rao", "Siobhán Ní Bhriain", "Speaker 2"]
    # "Note: " and "Timeline: " inside speech did not split a turn; the document header is not a speaker
    assert parsed.turns[1]["text"] == "Agreed. Note: finance signs off first."
    assert parsed.turns[2]["text"] == "One question. Timeline: six weeks?"
    assert parsed.turns[6]["t_start"] == 12 * 60 + 5


def test_multiline_turns_and_label_alone_on_a_line():
    parsed = parsers.parse_plain("Asha Rao:\nFirst line\nsecond line\nMaya Iyer: ok")
    assert [(t["speaker_label"], t["text"]) for t in parsed.turns] == [
        ("Asha Rao", "First line\nsecond line"), ("Maya Iyer", "ok")]


def test_strict_only_splits_on_me_them_speaker():
    text = " Them: Good afternoon.  Me: Thanks. Note: I'll send it.\nAsha Rao: not a label here.  Speaker B: hello"
    assert [l for l, _, _ in parsers.labelled(text, strict=True)] == ["Them", "Me", "Speaker B"]
    assert "Asha Rao: not a label here." in parsers.labelled(text, strict=True)[1][1]


def test_paste_parse_keeps_its_old_contract():
    assert paste.parse("Me: Hello.\nRavi: Namaste.\nThem: Hi.") == [
        ("me", "me", "Hello."), ("them", "Ravi", "Namaste."), ("them", "them_1", "Hi.")]
    # Granola's single string, labels after two spaces
    assert paste.parse(" Them: Sir. Good afternoon.  Me: Hello.") == [
        ("them", "them_1", "Sir. Good afternoon."), ("me", "me", "Hello.")]


def test_otter():
    parsed = parsers.parse_any(OTTER, "otter.txt")
    assert parsed.format == "otter"
    assert labels(parsed) == ["Asha Rao", "Maya Iyer", "Jean-Luc O'Neil"]
    assert parsed.turns[0]["text"] == "Good afternoon. Can you hear me? We had some trouble with the line."
    assert parsed.turns[1]["text"].endswith("Note: I'll send the deck tomorrow.")
    assert (parsed.turns[0]["t_start"], parsed.turns[0]["t_end"]) == (3.0, 12.0)
    assert parsed.turns[2]["t_start"] == 3600 + 5 * 60 + 40


def test_vtt_voice_tags_entities_and_name_prefix():
    parsed = parsers.parse_any(VTT.encode(), "meeting.vtt")
    assert parsed.format == "vtt"
    assert [(t["speaker_label"], t["text"]) for t in parsed.turns] == [
        ("Asha Rao", "Good afternoon, can you hear me?"),           # two cues, one utterance
        ("Maya Iyer", "Loud & clear."),
        ("Dr. A. K. Sharma", "We need the numbers by site.")]
    assert (parsed.turns[0]["t_start"], parsed.turns[0]["t_end"]) == (1.0, 6.0)
    assert parsed.turns[2]["t_start"] == 70.0


def test_vtt_without_the_extension_is_sniffed():
    assert parsers.parse_any(VTT).format == "vtt"
    assert parsers.parse_any(SRT).format == "srt"


def test_srt_multiline_bracket_names_and_hours():
    parsed = parsers.parse_any(SRT, "captions.srt")
    assert parsed.format == "srt"
    assert [(t["speaker_label"], t["text"]) for t in parsed.turns] == [
        ("Asha Rao", "Good afternoon."), ("Maya Iyer", "Thanks. I will share the proposal on Tuesday."),
        ("María-José Núñez", "Gracias a todos.")]
    assert parsed.turns[1]["t_end"] == 7.25 and parsed.turns[2]["t_start"] == 3723.0


def test_long_caption_runs_are_cut_into_citeable_turns():
    cues = "\n\n".join(f"00:00:{i:02d}.000 --> 00:00:{i + 1:02d}.000\n<v Asha Rao>{'word ' * 12}</v>" for i in range(40))
    parsed = parsers.parse_vtt("WEBVTT\n\n" + cues)
    assert len(parsed.turns) > 1 and all(len(t["text"]) <= parsers.MAX_TURN_CHARS for t in parsed.turns)


def test_fireflies_export_sentences_become_turns():
    parsed = parsers.parse_any(json.dumps(FIREFLIES), "fireflies.json")
    assert parsed.format == "fireflies" and (parsed.ext_source, parsed.ext_id) == ("fireflies", "ff-01HZX")
    assert [(t["speaker_label"], t["text"]) for t in parsed.turns] == [
        ("Asha Rao", "Good afternoon. We need lane data first."),
        ("Maya Iyer", "I'll send the lane template by Thursday."), ("Asha Rao", "Fine.")]
    assert (parsed.turns[0]["t_start"], parsed.turns[0]["t_end"]) == (1.2, 5.0)
    assert parsed.started_at == "2026-09-02T10:00:00+00:00" and parsed.ended_at == "2026-09-02T10:31:30+00:00"
    assert {p["email"] for p in parsed.participants} == {"maya@tessel.test", "asha.rao@acmefreight.test"}
    assert parsed.summary == "Asha wants lane data first."
    # the bare sentence list some exports contain, with the other key spelling
    bare = [{"sentence": "Hello.", "speaker_name": "Asha Rao", "startTime": "00:01", "endTime": "00:02"}]
    assert parsers.parse_any(json.dumps(bare)).turns == [
        {"speaker_label": "Asha Rao", "text": "Hello.", "t_start": 1.0, "t_end": 2.0}]
    # and the API envelope
    assert parsers.parse_json({"data": {"transcript": FIREFLIES}}).ext_id == "ff-01HZX"


def test_fathom_export():
    parsed = parsers.parse_any(json.dumps({**FATHOM_MEETING, "transcript": FATHOM_TRANSCRIPT}), "fathom.json")
    assert parsed.format == "fathom" and parsed.ext_id == "4471203"
    assert labels(parsed) == ["Asha Rao", "S. Jain", "Asha Rao"]
    assert parsed.turns[1]["t_start"] == 11.0 and parsed.turns[2]["t_start"] == 62.0
    assert parsed.title == "Acme pilot review" and parsed.started_at == "2026-09-16T09:31:10+00:00"
    assert "Pilot scope agreed" in parsed.summary
    assert parsers.parse_json(FATHOM_TRANSCRIPT).format == "fathom"          # a bare transcript list


def test_generic_schema():
    parsed = parsers.parse_any(json.dumps(GENERIC), "anything.json")
    assert parsed.format == "generic" and (parsed.ext_source, parsed.ext_id) == ("otter", "zap-7781")
    assert parsed.started_at == "2026-09-16T09:30:00+00:00"
    assert [t.get("t_start") for t in parsed.turns] == [3.0, 10.0, 16.0]
    assert parsed.turns[1]["t_end"] == 15.0 and "t_end" not in parsed.turns[2]


@pytest.mark.parametrize("data,name,needle", [
    (b"\x00\x01RIFF\x00\x00WAVEfmt " + b"\x00" * 64, "call.wav", "not text"),
    (b"Just some notes about a meeting with nobody labelled.", "notes.txt", "no speaker turns found"),
    (b'{"hello": "world"}', "x.json", "not a transcript the coach knows"),
    (b'{"turns": [', "broken.json", "does not parse"),
    (b'{"turns": []}', "empty.json", "no speaker turns found"),
])
def test_unrecognised_files_say_why(data, name, needle):
    with pytest.raises(parsers.UnrecognisedTranscript) as err:
        parsers.parse_any(data, name)
    assert needle in str(err.value)


def test_seconds_and_iso_are_tolerant():
    assert parsers.seconds("01:02:03,250") == 3723.25 and parsers.seconds("bad") is None and parsers.seconds(-1) is None
    assert parsers.iso(1788343200) == parsers.iso(1788343200000) == "2026-09-02T10:00:00+00:00"
    assert parsers.iso("2026-09-16 15:00") == "2026-09-16T09:30:00+00:00"    # no zone: the seller's (Asia/Kolkata)
    assert parsers.iso("yesterday") is None


def test_named_fixture_is_plain():
    assert parsers.parse_any(NAMED).format == "plain" and len(parsers.parse_any(NAMED).turns) == 7
