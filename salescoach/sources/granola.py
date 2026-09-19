"""Granola backfill: bring past calls in verbatim, as lower-trust history.

Python cannot reach the Granola connector (it lives behind Claude). So a
`claude -p` session is allowed exactly the Granola read tools and asked to
call them and answer "DONE". With --output-format stream-json the tool
RESULTS travel in the output stream, and this module reads them straight from
there (spike 2026-09-11: 3.2 KB meeting metadata, 20 KB transcript, verbatim).
No model ever retypes a transcript.

Imported calls are source='granola' and history=1: the pipeline analyses them and they
seed deals, loops and seller patterns, but they never get a drafted follow-up, and
jarvis_bridge does not claim them (Jarvis already processed the original meeting). It is
the history flag that carries that policy, not the name. Granola's own AI summary is kept as
a separate 'granola_summary' artifact, labelled third-party inference.

Granola labels by audio path ("Me" is the microphone), so its channels are taken as given
and never held for the "which speaker are you?" question.

Tool results are untrusted text. They are only ever parsed, never followed.
"""
import html
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

from .. import config, providers
from . import base, parsers

TOOLS = "mcp__claude_ai_Granola__"


class GranolaError(RuntimeError):
    pass


def _claude_tools(prompt: str, tools: list[str], timeout: int = 300) -> list[tuple[str, bool, str]]:
    sandbox = config.RUNTIME_DIR / "granola-sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    binary = providers.claude_cli_binary()
    # --setting-sources project keeps plugins and user hooks out; the claude.ai
    # Granola connector still loads (checked 2026-09-11).
    cmd = [binary, "-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "project",
           "--no-session-persistence", "--disable-slash-commands", "--model", "haiku",
           "--allowedTools", ",".join(TOOLS + t for t in tools)]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=sandbox)
    except subprocess.TimeoutExpired as exc:
        raise GranolaError(f"Granola fetch timed out after {exc.timeout}s") from exc
    return parse_stream(proc.stdout)


def parse_stream(stdout: str) -> list[tuple[str, bool, str]]:
    names, results = {}, []
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (message.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                names[block["id"]] = block["name"]
            elif block.get("type") == "tool_result":
                body = block.get("content")
                text = "".join(c.get("text", "") for c in body) if isinstance(body, list) else str(body or "")
                results.append((names.get(block.get("tool_use_id"), ""), bool(block.get("is_error")), text))
    return results


_PERSISTED = re.compile(r"^<persisted-output>\s*Output too large[^\n]*Full output saved to: (\S+\.json)")
PERSIST_ROOT = os.path.realpath(os.path.expanduser("~/.claude/projects"))


def _unpersist(text: str) -> str:
    """Claude Code replaces a large tool result (a 61.8 KB leadership
    transcript) with a 2 KB preview and saves the full result to a JSON file.
    Read that file instead of the preview. Only a .json under ~/.claude/projects
    is accepted, so text inside a transcript cannot point the read elsewhere."""
    m = _PERSISTED.match(text or "")
    if not m:
        return text
    path = os.path.realpath(m.group(1))
    if not path.startswith(PERSIST_ROOT + os.sep) or not os.path.isfile(path):
        raise GranolaError("large Granola result was saved somewhere unexpected; refusing to read it")
    with open(path) as fh:
        blocks = json.load(fh)
    if isinstance(blocks, list):
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    return str(blocks)


def _result(results, tool) -> str:
    for name, is_error, text in results:
        if name == TOOLS + tool:
            if is_error:
                raise GranolaError(f"{tool} failed: {text[:300]}")
            return _unpersist(text)
    raise GranolaError(f"{tool} was not called; the Granola connector may need re-authorising")


_MEETING = re.compile(r'<meeting id="([^"]+)" title="([^"]*)" date="([^"]*)"[^>]*>(.*?)</meeting>', re.S)
_PARTICIPANTS = re.compile(r"<known_participants>(.*?)</known_participants>", re.S)
_PERSON = re.compile(r"\s*([^,<]+?)\s*(?:\(note creator\))?\s*(?:from\s+([^<]+?))?\s*<([^>]+)>")
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)


def parse_meetings(text: str) -> list[dict]:
    meetings = []
    for mid, title, date, body in _MEETING.findall(text):
        people_block = _PARTICIPANTS.search(body)
        people = []
        if people_block:
            for name, org, email in _PERSON.findall(html.unescape(people_block.group(1))):
                people.append({"name": name.replace("(note creator)", "").strip(), "org": (org or "").strip(),
                               "email": email.strip().lower()})
        summary = _SUMMARY.search(body)
        meetings.append({"id": mid, "title": html.unescape(title), "date": date, "participants": people,
                         "summary": summary.group(1).strip() if summary else None})
    return meetings


def parse_date(text: str):
    """'Sep 2, 2026 2:00 PM GMT+5:30' -> aware ISO timestamp, or None."""
    m = re.match(r"(\w{3} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)(?: GMT([+-])(\d{1,2})(?::(\d{2}))?)?", text or "")
    if not m:
        return None
    local = datetime.strptime(m.group(1), "%b %d, %Y %I:%M %p")
    offset = timedelta(0)
    if m.group(2):
        offset = timedelta(hours=int(m.group(3)), minutes=int(m.group(4) or 0))
        offset = offset if m.group(2) == "+" else -offset
    return local.replace(tzinfo=timezone(offset)).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_transcript(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise GranolaError("no transcript JSON in tool result")
    # Granola emits raw newlines/tabs inside the transcript string (the 25 Aug
    # real meeting did), which strict JSON rejects. raw_decode also ignores
    # anything the connector appends after the object.
    obj, _ = json.JSONDecoder(strict=False).raw_decode(text[start:])
    return obj.get("transcript") or ""


def list_meetings(time_range: str = "last_30_days") -> list[dict]:
    prompt = (f'Call {TOOLS}list_meetings with {{"time_range": "{time_range}", '
              '"involvement": {"captured_by_me": true}}. Do not repeat its content. Then reply with exactly: DONE')
    return parse_meetings(_result(_claude_tools(prompt, ["list_meetings"]), "list_meetings"))


def fetch(meeting_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", meeting_id):
        raise GranolaError("meeting id must be a UUID")
    prompt = (f'Call {TOOLS}get_meetings with meeting_ids ["{meeting_id}"], then call '
              f'{TOOLS}get_meeting_transcript with meeting_id "{meeting_id}". Do not repeat or summarise '
              "their content. After both calls, reply with exactly: DONE")
    results = _claude_tools(prompt, ["get_meetings", "get_meeting_transcript"])
    meta_text = _result(results, "get_meetings")
    transcript_text = _result(results, "get_meeting_transcript")
    meetings = parse_meetings(meta_text)
    meeting = meetings[0] if meetings else {"id": meeting_id, "title": "Granola meeting", "date": "",
                                            "participants": [], "summary": None}
    meeting["transcript"] = parse_transcript(transcript_text)
    meeting["raw"] = {"get_meetings": meta_text, "get_meeting_transcript": transcript_text}
    return meeting


def me_addresses(conn=None) -> set[str]:
    """The seller's own addresses (profile, plus the is_me row): that participant is ME, not a buyer."""
    return base.me_addresses(conn)


def normalize(data: dict, meeting_id: str) -> base.NormalizedTranscript:
    """A fetched meeting as a NormalizedTranscript. Only Me / Them / Speaker X are labels, and the
    channel is Granola's own: 'Me' is what the microphone heard."""
    turns = [{"speaker_label": label, "text": body, "channel": "me" if label.lower() == "me" else "them"}
             for label, body, _ in parsers.labelled(data.get("transcript") or "", strict=True)]
    started = parse_date(data.get("date", ""))
    return base.NormalizedTranscript(
        source_kind="granola", source_ref=f"granola:{meeting_id}", title=data.get("title") or "Granola meeting",
        started_at=started, ended_at=started, turns=turns, summary=data.get("summary") or None,
        participants=[{"name": p.get("name"), "email": p.get("email")} for p in data.get("participants", [])],
        raw=data["raw"] if "raw" in data else data)


def import_meeting(conn, meeting_id: str, deal_id=None, lang_mode="auto", data: dict | None = None,
                   history: bool = True) -> str:
    """Import one Granola meeting as a call. Idempotent on the Granola id. history=True (the
    backfill) means no follow-up is drafted; the sources poller passes False for new meetings."""
    existing = base.existing_call(conn, f"granola:{meeting_id}")
    if existing:
        return existing
    data = data or fetch(meeting_id)
    return base.import_normalized(conn, normalize(data, meeting_id), deal_id=deal_id, history=history,
                                  lang_mode=lang_mode).call_id
