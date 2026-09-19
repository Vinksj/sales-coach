"""Transcript parsers, one per export format, behind one sniffing front door.

  parse_any(data, filename=None) -> Parsed        sniff the format, parse, or raise UnrecognisedTranscript
  labelled(text, strict=False)   -> [(label, text, t_start)]   the plain "Name: text" splitter

Formats (docs/sources.md describes each with an example):
  plain      "Name: text" per line, or Granola's single string "  Me: ...  Them: ..."
  otter      "Name  0:12" on its own line, the words on the lines below it
  vtt        WebVTT, speakers as <v Name> voice tags or a "Name: " prefix (Zoom, Teams, Meet)
  srt        SubRip, speakers as a "Name: " prefix
  fireflies  Fireflies JSON (an export, or the API's transcript object)
  fathom     Fathom JSON (an export, or the API's meeting object)
  generic    THE schema for the webhook and the watched folder: {title, started_at, participants,
             turns: [{speaker, text, start, end}]}

A parser never decides who the seller is. It returns speaker LABELS exactly as the recorder wrote
them; sources/base.py maps labels to channels from the seller profile. Everything parsed here is
untrusted text: it is only ever split and stored, never followed.
"""
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

MAX_TURN_CHARS = 600          # captions arrive a few words at a time; merged turns stay citeable
MAX_INPUT_BYTES = 5 * 1024 * 1024    # one transcript, by ANY path (paste, upload, folder, webhook, an API)
MAX_SECONDS = 7 * 24 * 3600   # a turn offset beyond a week is not an offset (and overflows a timedelta)


class UnrecognisedTranscript(ValueError):
    """The file is not in any format the coach reads. The message says what would work."""


@dataclass
class Parsed:
    format: str
    turns: list = field(default_factory=list)          # {speaker_label, text, t_start?, t_end?, channel?}
    title: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    participants: list = field(default_factory=list)   # {name, email}
    summary: Optional[str] = None
    ext_source: Optional[str] = None                   # the recorder the payload names ("fireflies", "otter")
    ext_id: Optional[str] = None                       # the meeting's id at that recorder


# ------------------------------------------------------------------------------------------ labels
# A person's name as recorders print it: one to five words, each starting with a letter that is not
# lower case (so scripts without case pass), with dots, hyphens and apostrophes inside: "Asha",
# "J. R. Rao", "A.K. Sharma", "Mary-Jane O'Neil", "Siobhán Ní Bhriain", "अनीता शर्मा", "王伟".
# Lower-case particles are allowed between words ("Ludwig van der Berg", "Abdul bin Rashid").
# (Inside a word anything but space and delimiters is allowed: Python's \w leaves out the combining
# vowel signs of Devanagari, Tamil, Thai..., so "अनीता" is not \w+ although it is one word.)
_TOKEN = r"[^\W\d_][^\s:;,!?()\[\]<>\"“”|/\\]*"
_NAME = rf"{_TOKEN}(?:[ \t]{_TOKEN}){{0,4}}"
_GENERIC = r"Me|Them|Speaker[ \t]?[A-Z0-9]{1,3}"
_TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
_PARTICLES = frozenset({"de", "del", "della", "der", "den", "di", "da", "dos", "du", "la", "le", "van", "von",
                        "bin", "binti", "ibn", "al", "el", "e", "y", "ter", "ten", "af", "av", "zu"})
# Header words of an exported document. "Date: 3 Sep" at the top of a file is not somebody speaking.
_NOT_A_SPEAKER = frozenset({
    "date", "time", "title", "subject", "attendees", "participants", "duration", "location", "meeting",
    "agenda", "summary", "notes", "note", "transcript", "recording", "link", "url", "topic", "host",
    "organizer", "organiser", "re", "fwd", "fw", "to", "from", "cc", "action", "items", "next", "steps",
    "keywords", "http", "https", "source", "language", "created", "updated", "id", "start", "end",
})

# The two patterns the paste box has always used: a label at the start, after a line break, or after
# two spaces mid-line (Granola's single-string transcripts are split that way).
# Review 3: the prefix used to be `(?:^|\s{2,}|\n)\s*`, which consumes the whitespace run in every
# possible split: O(n^3) on a run of spaces, with the GIL held (1.6 KB of spaces = 10 s, 10 KB = most of
# an hour, the whole server frozen). The lookbehind says the same thing ("start, or a line break, or two
# whitespace characters just before the label") without consuming anything, so it is linear.
_LABEL_PREFIX = r"(?:^|(?<=\n)|(?<=\s\s))"
_OLD_LABEL = re.compile(_LABEL_PREFIX + r"(Me|Them|Speaker [A-Z]|[A-Z][a-zA-Z.]+(?: [A-Z][a-zA-Z.]+)?):\s+")
# Granola only ever labels Me / Them / Speaker X. Accepting capitalised words there would split a
# turn at any "Note: " inside the speech.
STRICT_LABEL = re.compile(_LABEL_PREFIX + r"(Me|Them|Speaker [A-Z]):\s+")
# The wider pattern only ever matches at the START OF A LINE: a three-word or hyphenated or
# non-Latin name, optionally with a timestamp before or after it and a company in brackets.
_LINE_LABEL = re.compile(
    rf"(?:^|\n)[ \t]*(?:[\[(]?(?P<ts>{_TS})[\])]?[ \t]+(?:[-–][ \t]+)?)?"
    rf"(?P<label>{_GENERIC}|{_NAME})"
    rf"(?:[ \t]*\((?P<paren>[^()\n]{{1,40}})\))?(?:[ \t]*[\[(]?(?P<ts2>{_TS})[\])]?)?"
    rf"[ \t]*:(?:[ \t]+|(?=\n)|$)")
_OTTER_HEAD = re.compile(
    rf"^[ \t]*(?P<label>{_GENERIC}|Unknown Speaker|{_NAME})[ \t]+(?P<ts>\d{{1,2}}:\d{{2}}(?::\d{{2}})?)[ \t]*$")


def looks_like_name(label: str) -> bool:
    label = " ".join((label or "").split())
    if not label or len(label) > 60:
        return False
    if re.fullmatch(_GENERIC, label):
        return True
    words = label.split(" ")
    if len(words) > 5 or all(w.casefold().strip(".") in _NOT_A_SPEAKER for w in words):
        return False
    for i, word in enumerate(words):
        if word[0].islower() and not (0 < i < len(words) - 1 and word.casefold() in _PARTICLES):
            return False
    return True


def seconds(value) -> Optional[float]:
    """12.5, "12.5", "0:12", "01:02:03.250", "00:01:02,250" -> seconds. Anything else -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if 0 <= value <= MAX_SECONDS else None      # also drops nan and inf
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    if re.fullmatch(r"\d{1,9}(?:\.\d{1,9})?", text):
        found = float(text)
        return found if found <= MAX_SECONDS else None
    m = re.fullmatch(r"(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2}(?:\.\d{1,9})?)", text)
    if not m:
        return None
    found = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return found if found <= MAX_SECONDS else None


def iso(value) -> Optional[str]:
    """An epoch (seconds or milliseconds) or an ISO-8601 string -> aware UTC ISO timestamp.
    A time without a zone is read in the seller's zone, as the import form reads it."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d{9,14}(?:\.\d+)?", str(value).strip()):
            number = float(value)
            ts = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc)
        else:
            ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if ts.tzinfo is None:
                from .. import seller
                ts = ts.replace(tzinfo=seller.zone())
    except (ValueError, OverflowError, OSError):
        return None
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def labelled(text: str, strict: bool = False) -> list:
    """Split "Label: words" text into (label, words, t_start) turns.

    strict: only Me / Them / Speaker X are labels (Granola). Otherwise the long-standing paste
    pattern (anywhere after a line break or two spaces) plus, at the start of a line only, the wide
    name pattern. Text before the first label (a document header) is dropped."""
    check_size(text)
    text = "\n" + (text or "").strip()
    found = []                                         # (start, end, label, t_start)
    if strict:
        found = [(m.start(), m.end(), m.group(1), None) for m in STRICT_LABEL.finditer(text)]
    else:
        for m in _OLD_LABEL.finditer(text):
            if looks_like_name(m.group(1)):
                found.append((m.start(), m.end(), m.group(1), None))
        for m in _LINE_LABEL.finditer(text):
            label = " ".join(m.group("label").split())
            if not looks_like_name(label):
                continue
            stamp = m.group("ts") or m.group("ts2")
            paren = m.group("paren")
            if paren and stamp is None and seconds(paren) is not None:
                stamp = paren
            found.append((m.start(), m.end(), label, seconds(stamp)))
        # Both patterns see the same line start: keep the one that starts first, then the longer.
        found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
        kept = []
        for f in found:
            if kept and f[0] < kept[-1][1]:
                continue
            kept.append(f)
        found = kept
    turns = []
    for i, (_, end, label, t_start) in enumerate(found):
        stop = found[i + 1][0] if i + 1 < len(found) else len(text)
        body = text[end:stop].strip()
        if body:
            turns.append((label, body, t_start))
    return turns


def check_size(data) -> None:
    """The one size limit, whatever the door: the paste box and the upload form get the same 5 MB the
    webhook and the watched folder have. (A three-hour call is well under 1 MB of text.)"""
    if data is not None and len(data) > MAX_INPUT_BYTES:
        raise UnrecognisedTranscript(f"this transcript is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB; a "
                                     "transcript never is (a recording goes through Upload a recording)")


def _turn(label, text, t_start=None, t_end=None, channel=None) -> dict:
    turn = {"speaker_label": " ".join(str(label or "").split()), "text": str(text or "").strip()}
    if t_start is not None:
        turn["t_start"] = t_start
    if t_end is not None:
        turn["t_end"] = t_end
    if channel in ("me", "them"):
        turn["channel"] = channel
    return turn


def merge_runs(turns: list, max_chars: int = MAX_TURN_CHARS) -> list:
    """Caption cues and sentence lists break one utterance into many rows. Consecutive rows by the
    same speaker become one turn, up to max_chars so a quote can still be cited to a small turn."""
    merged = []
    for t in turns:
        if not t["text"]:
            continue
        last = merged[-1] if merged else None
        if (last and last["speaker_label"] == t["speaker_label"] and last.get("channel") == t.get("channel")
                and len(last["text"]) + len(t["text"]) < max_chars):
            last["text"] = f"{last['text']} {t['text']}"
            if t.get("t_end") is not None or t.get("t_start") is not None:
                last["t_end"] = t.get("t_end", t.get("t_start"))
            continue
        merged.append(dict(t))
    return merged


# ------------------------------------------------------------------------------------ text formats

def parse_plain(text: str, strict: bool = False) -> Parsed:
    return Parsed("plain", [_turn(label, body, t) for label, body, t in labelled(text, strict=strict)])


def parse_otter(text: str) -> Parsed:
    turns, current = [], None
    for line in (text or "").splitlines():
        head = _OTTER_HEAD.match(line)
        if head and looks_like_name(head.group("label")):
            current = _turn(head.group("label"), "", seconds(head.group("ts")))
            turns.append(current)
        elif current is not None and line.strip():
            current["text"] = f"{current['text']} {line.strip()}".strip()
    turns = [t for t in turns if t["text"]]
    for a, b in zip(turns, turns[1:]):
        if b.get("t_start") is not None and a.get("t_start") is not None and b["t_start"] >= a["t_start"]:
            a["t_end"] = b["t_start"]
    return Parsed("otter", turns)


_CUE_TIME = re.compile(rf"^\s*({_TS})\s*-->\s*({_TS})")
# Review 3: every class below excludes "<" and is bounded, and a voice tag's .class list cannot be split
# two ways. Before, "<a<a<a..." was quadratic and "<v.a.a.a...>" exponential (24 classes = 0.7 s, 40 = years).
_VOICE = re.compile(r"<v(?:\.[^\s<>.]{1,60}){0,8}[ \t]+([^<>]{1,200})>", re.I)
_TAG = re.compile(r"</?[a-zA-Z][^<>]{0,200}>|<\d{1,2}:\d{2}[^<>]{0,40}>")
_CUE_LABEL = re.compile(rf"^\s*(?:-\s*|>>\s*)?(?:\[(?P<b>{_NAME})\]|(?P<l>{_GENERIC}|{_NAME})[ \t]*:)[ \t]+(?P<rest>.+)$", re.S)


def _parse_cues(text: str, fmt: str) -> Parsed:
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿"))
    turns, speaker = [], ""
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        at = next((i for i, l in enumerate(lines) if _CUE_TIME.match(l)), None)
        if at is None:
            continue                                   # WEBVTT header, NOTE, STYLE, REGION
        timing = _CUE_TIME.match(lines[at])
        payload = " ".join(l.strip() for l in lines[at + 1:])
        if not payload:
            continue
        voice = _VOICE.search(payload)
        body = html.unescape(_TAG.sub("", payload)).strip()
        if voice:
            speaker = html.unescape(voice.group(1)).strip()
        else:
            m = _CUE_LABEL.match(body)
            label = m and (m.group("b") or m.group("l"))
            if label and looks_like_name(label):
                speaker, body = label, m.group("rest").strip()
        turns.append(_turn(speaker, body, seconds(timing.group(1)), seconds(timing.group(2))))
    return Parsed(fmt, merge_runs(turns))


def parse_vtt(text: str) -> Parsed:
    return _parse_cues(text, "vtt")


def parse_srt(text: str) -> Parsed:
    return _parse_cues(text, "srt")


# ------------------------------------------------------------------------------------ JSON formats

def _first(obj: dict, *keys, default=None):
    for key in keys:
        if isinstance(obj, dict) and obj.get(key) not in (None, ""):
            return obj[key]
    return default


def _scalar(value, limit: int = 300) -> Optional[str]:
    """A title (or any one-line field) from a payload: text or a number, else nothing. A list or a
    mapping where a title belongs is somebody else's schema, not a title."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return " ".join(str(value).split())[:limit] or None


def _people(items) -> list:
    """[{name,email}] from whatever a recorder calls its attendee list. Strings may be bare emails."""
    out, seen = [], set()
    for item in items if isinstance(items, (list, tuple)) else []:
        if isinstance(item, str):
            name, email = ("", item) if "@" in item else (item, "")
        elif isinstance(item, dict):
            name = _first(item, "name", "displayName", "display_name", default="")
            email = _first(item, "email", "email_address", "matched_calendar_invitee_email", default="")
        else:
            continue
        name, email = str(name or "").strip(), str(email or "").strip().lower()
        if email and not re.fullmatch(r"[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+", email):
            email = ""
        key = email or name.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": name or email.split("@", 1)[0], "email": email or None})
    return out


def fireflies_to_parsed(obj) -> Parsed:
    """A Fireflies transcript: the API's `transcript` object, or the JSON export (sometimes only the
    sentence list). Sentences are merged into turns per speaker. The single place that knows the
    Fireflies field names."""
    if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
        obj = obj["data"].get("transcript") or obj["data"]
    if not isinstance(obj, (dict, list)):
        raise UnrecognisedTranscript("this looks like a Fireflies answer, but its transcript is not an object")
    sentences = obj if isinstance(obj, list) else obj.get("sentences")
    sentences = sentences if isinstance(sentences, list) else []
    meta = obj if isinstance(obj, dict) else {}
    turns = []
    for s in sentences:
        if not isinstance(s, dict):
            continue
        turns.append(_turn(_first(s, "speaker_name", "speakerName", "speaker", default=""),
                           _first(s, "text", "sentence", "raw_text", default=""),
                           seconds(_first(s, "start_time", "startTime", "start")),
                           seconds(_first(s, "end_time", "endTime", "end"))))
    people = _people(meta.get("meeting_attendees")) or _people(meta.get("participants"))
    known = {p["email"] for p in people if p["email"]}
    people += [p for p in _people(meta.get("participants")) if p["email"] and p["email"] not in known]
    summary = meta.get("summary")
    if isinstance(summary, dict):
        summary = _first(summary, "overview", "short_summary", "shorthand_bullet")
    started = iso(_first(meta, "date", "dateString", "date_string"))
    duration = meta.get("duration")                    # minutes, per the API docs
    ended = None
    if started and isinstance(duration, (int, float)) and duration > 0:
        ended = iso(datetime.fromisoformat(started).timestamp() + float(duration) * 60)
    return Parsed("fireflies", merge_runs(turns), title=_scalar(_first(meta, "title")), started_at=started, ended_at=ended,
                  participants=people, summary=summary if isinstance(summary, str) else None,
                  ext_source="fireflies", ext_id=str(meta["id"]) if meta.get("id") else None)


def fathom_to_parsed(obj) -> Parsed:
    """A Fathom meeting: the API's meeting object with `transcript`, or a bare transcript list.
    The single place that knows the Fathom field names."""
    meta = obj if isinstance(obj, dict) else {}
    items = obj if isinstance(obj, list) else meta.get("transcript")
    items = items if isinstance(items, list) else []
    turns, speakers = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        who = item.get("speaker")
        if isinstance(who, dict):
            label = _first(who, "display_name", "name", default="")
            speakers.append({"name": label, "email": who.get("matched_calendar_invitee_email")})
        else:
            label = who or ""
        turns.append(_turn(label, item.get("text"), seconds(_first(item, "timestamp", "start_time", "start")),
                           seconds(_first(item, "end_time", "end"))))
    people = _people(meta.get("calendar_invitees"))
    recorder = meta.get("recorded_by")
    known = {p["email"] for p in people if p["email"]}
    for extra in _people([recorder] if isinstance(recorder, dict) else []):
        if extra["email"] and extra["email"] not in known:
            known.add(extra["email"])
            people.append(extra)
    # Fathom matches each speaker to an invitee's address. A speaker shown under another name than the
    # invite's ("S. Jain") is kept as a second entry for that address: it is how the label finds its person.
    pairs = {(p["name"].casefold(), p["email"]) for p in people}
    for who in speakers:
        name, email = str(who.get("name") or "").strip(), str(who.get("email") or "").strip().lower()
        if name and email and (name.casefold(), email) not in pairs:
            pairs.add((name.casefold(), email))
            people.append({"name": name, "email": email})
    summary = meta.get("default_summary")
    if isinstance(summary, dict):
        summary = _first(summary, "markdown_formatted", "text")
    ext_id = _first(meta, "recording_id", "id")
    return Parsed("fathom", merge_runs(turns), title=_scalar(_first(meta, "title", "meeting_title")),
                  started_at=iso(_first(meta, "recording_start_time", "scheduled_start_time", "created_at")),
                  ended_at=iso(_first(meta, "recording_end_time", "scheduled_end_time")),
                  participants=people, summary=summary if isinstance(summary, str) else None,
                  ext_source="fathom", ext_id=str(ext_id) if ext_id else None)


def generic_to_parsed(obj: dict) -> Parsed:
    """THE schema (docs/sources.md): {id?, source?, title?, started_at?, ended_at?, summary?,
    participants: [{name, email}], turns: [{speaker, text, start?, end?, channel?}]}."""
    turns = []
    for item in obj.get("turns") if isinstance(obj.get("turns"), list) else []:
        if not isinstance(item, dict):
            continue
        turns.append(_turn(_first(item, "speaker", "speaker_label", "speaker_name", "name", default=""),
                           item.get("text"), seconds(_first(item, "start", "t_start", "start_time")),
                           seconds(_first(item, "end", "t_end", "end_time")), channel=item.get("channel")))
    source = re.sub(r"[^a-z0-9_-]", "", str(obj.get("source") or "").lower())[:30] or None
    ext_id = obj.get("id")
    return Parsed("generic", [t for t in turns if t["text"]], title=_scalar(_first(obj, "title")),
                  started_at=iso(obj.get("started_at")), ended_at=iso(obj.get("ended_at")),
                  participants=_people(obj.get("participants")),
                  summary=obj.get("summary") if isinstance(obj.get("summary"), str) else None,
                  ext_source=source, ext_id=str(ext_id)[:200] if ext_id not in (None, "") else None)


def parse_json(obj) -> Parsed:
    if isinstance(obj, dict) and isinstance(obj.get("data"), dict) and "transcript" in obj["data"]:
        return fireflies_to_parsed(obj)
    rows = obj if isinstance(obj, list) else None
    if isinstance(obj, dict):
        if isinstance(obj.get("turns"), list):
            return generic_to_parsed(obj)
        if isinstance(obj.get("sentences"), list):
            return fireflies_to_parsed(obj)
        if isinstance(obj.get("transcript"), list):
            return fathom_to_parsed(obj)
        if isinstance(obj.get("transcript"), str):     # {"title":..., "transcript": "Me: ...  Them: ..."}
            parsed = parse_plain(obj["transcript"])
            parsed.title = _scalar(_first(obj, "title"))
            parsed.started_at = iso(_first(obj, "started_at", "date"))
            return parsed
    if rows and all(isinstance(r, dict) for r in rows):
        if any("speaker_name" in r or "sentence" in r for r in rows):
            return fireflies_to_parsed(rows)
        if any(isinstance(r.get("speaker"), dict) for r in rows):
            return fathom_to_parsed(rows)
        if any("text" in r for r in rows):
            return generic_to_parsed({"turns": rows})
    raise UnrecognisedTranscript(
        "this JSON is not a transcript the coach knows: expected {\"turns\": [{\"speaker\", \"text\"}]} "
        "(docs/sources.md), a Fireflies export (\"sentences\") or a Fathom export (\"transcript\")")


# -------------------------------------------------------------------------------------- front door

def sniff(text: str, filename: Optional[str] = None) -> str:
    head = (text or "").lstrip("﻿ \t\r\n")
    suffix = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if head[:1] in "{[" and (suffix == "json" or _is_json(head)):
        return "json"
    if head.upper().startswith("WEBVTT") or suffix == "vtt":
        return "vtt"
    if suffix == "srt" or re.search(r"(?m)^\s*\d+\s*\n\s*\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->", head[:4000]):
        return "srt"
    if re.search(r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}\s*-->", head[:4000]):
        return "vtt"
    lines = head.splitlines()
    otter = sum(1 for l in lines if (m := _OTTER_HEAD.match(l)) and looks_like_name(m.group("label")))
    if otter and otter >= len(labelled(head)):
        return "otter"
    return "plain"


def load_json(text: str):
    """json.loads, with the one failure it does not report as a ValueError turned into one: a payload
    nested a hundred thousand levels deep is a RecursionError, which no caller of a parser expects."""
    try:
        return json.loads(text)
    except RecursionError:
        raise ValueError("the JSON is nested too deeply to be a transcript") from None


def _is_json(text: str) -> bool:
    try:
        load_json(text)
    except ValueError:
        return False
    return True


def decode(data) -> str:
    if isinstance(data, str):
        return data
    not_text = UnrecognisedTranscript("this file is not text: export the transcript as .txt, .vtt, .srt or .json "
                                      "(a recording goes through Upload a recording instead)")
    try:
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if b"\x00" in data[:4000]:
        raise not_text
    return data.decode("cp1252", errors="replace")      # an old Windows export with curly quotes


def parse_any(data, filename: Optional[str] = None) -> Parsed:
    """Sniff and parse. Raises UnrecognisedTranscript with a message the user can act on."""
    check_size(data)
    text = decode(data)
    if "\x00" in text[:2000]:
        raise UnrecognisedTranscript("this file is not text: export the transcript as .txt, .vtt, .srt or .json")
    fmt = sniff(text, filename)
    if fmt == "json":
        try:
            obj = load_json(text.lstrip("﻿"))
        except ValueError as exc:
            raise UnrecognisedTranscript(f"this .json file does not parse: {exc}") from None
        parsed = parse_json(obj)
    else:
        parsed = {"vtt": parse_vtt, "srt": parse_srt, "otter": parse_otter, "plain": parse_plain}[fmt](text)
    if not parsed.turns:
        raise UnrecognisedTranscript(
            "no speaker turns found. The coach reads: lines like \"Name: what they said\", Otter text exports, "
            ".vtt and .srt captions, Fireflies and Fathom JSON, and the generic JSON schema in docs/sources.md")
    return parsed
