"""claude.ai connectors (Google Calendar) reached through `claude -p`, read-only.

Python cannot call a claude.ai connector directly; it lives behind Claude. So,
as with the Granola backfill (sources/granola.py), a `claude -p` session is
allowed exactly one read tool, asked to call it and answer DONE, and the tool
RESULT is read verbatim from the stream-json output. No model retypes calendar
data.

Read-only is enforced three ways: this module refuses to build a call for any
tool outside READ_TOOLS; --allowedTools names only that one tool (plus
ToolSearch, which the CLI needs to load a deferred MCP tool's schema); and
--disallowedTools names every write tool the connector offers.

Tool names come from the session's init message (the list the CLI hands the
model), not from a model's answer. They are cached in the state table and
re-discovered weekly or on demand. Found 2026-09-12:
mcp__claude_ai_Google_Calendar__{list_events,list_calendars,get_event,
search_events,suggest_time} plus four write tools.
"""
import json
import subprocess

from .. import config, providers
from ..sources.granola import GranolaError, _unpersist, parse_stream
from ..store.stores import get_state, now, set_state
from . import common

SERVER_KEY = "google_calendar"
READ_TOOLS = {"list_events", "list_calendars", "get_event", "search_events", "suggest_time"}
WRITE_TOOLS = {"create_event", "update_event", "delete_event", "respond_to_event"}
STATE_KEY = "automation:calendar_tools"
MAX_AGE_DAYS = 7


class ConnectorError(RuntimeError):
    pass


def _run(prompt: str, allowed: list[str], disallowed: list[str], timeout: int) -> str:
    sandbox = config.RUNTIME_DIR / "connector-sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    binary = providers.claude_cli_binary()
    # --setting-sources project keeps plugins and user hooks out; claude.ai connectors still load.
    cmd = [binary, "-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "project",
           "--no-session-persistence", "--disable-slash-commands", "--model", "haiku"]
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    if disallowed:
        cmd += ["--disallowedTools", ",".join(disallowed)]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=sandbox)
    except FileNotFoundError as exc:
        raise ConnectorError(f"claude CLI not found at {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConnectorError(f"claude -p timed out after {exc.timeout}s") from exc
    return proc.stdout


def init_tools(stdout: str) -> list[str]:
    """The tool names from a stream-json session's init message."""
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("type") == "system" and message.get("subtype") == "init":
            return list(message.get("tools") or [])
    return []


def calendar_tools(tools: list[str]) -> list[str]:
    """The Google Calendar connector's tools among everything the session offers.

    By name first (mcp__claude_ai_Google_Calendar__*); failing that, the one MCP
    server whose tools include list_events and list_calendars, since a connector
    can also surface under an opaque id.
    """
    named = sorted(t for t in tools if SERVER_KEY in t.lower().replace("-", "_"))
    if named:
        return named
    by_prefix: dict[str, set] = {}
    for t in tools:
        prefix, sep, suffix = t.rpartition("__")
        if t.startswith("mcp__") and sep and prefix != "mcp":
            by_prefix.setdefault(prefix, set()).add(suffix)
    for prefix, suffixes in sorted(by_prefix.items()):
        if {"list_events", "list_calendars"} <= suffixes:
            return sorted(f"{prefix}__{s}" for s in suffixes)
    return []


def discover(conn=None, refresh: bool = False) -> dict:
    """{prefix, tools, discovered_at} for the Google Calendar connector."""
    from .. import identity
    if identity.cloud():
        raise ConnectorError("the Claude CLI calendar connector is not available in a cloud install "
                             "(per-user Google Calendar comes in Phase 5)")
    if conn is not None and not refresh:
        raw = get_state(conn, STATE_KEY)
        if raw:
            info = json.loads(raw)
            found = common.ts(info.get("discovered_at"))
            if found and (common.now_ist() - found).days < MAX_AGE_DAYS and info.get("tools"):
                return info
    tools = init_tools(_run("Reply with exactly: DONE", [], [], 120))
    if not tools:
        raise ConnectorError("claude -p reported no tools; is the CLI installed and logged in?")
    calendar = calendar_tools(tools)
    if not calendar:
        raise ConnectorError("the Google Calendar connector is not loaded in claude -p; "
                             "it may need authorising in claude.ai settings")
    info = {"prefix": calendar[0].rpartition("__")[0] + "__", "tools": calendar, "discovered_at": now()}
    if conn is not None:
        set_state(conn, STATE_KEY, json.dumps(info))
        conn.commit()
    return info


def call_read_tool(info: dict, tool: str, args: dict, timeout: int = 180) -> str:
    """Call one READ tool and return its result text verbatim."""
    if tool not in READ_TOOLS:
        raise ConnectorError(f"refused: {tool} is not a read-only tool")
    name = info["prefix"] + tool
    offered = info.get("tools") or []
    if name not in offered:
        raise ConnectorError(f"{name} is not offered by the connector")
    blocked = sorted({info["prefix"] + w for w in WRITE_TOOLS}
                     | {t for t in offered if t.rsplit("__", 1)[-1] not in READ_TOOLS})
    prompt = (f"Call {name} with {json.dumps(args)}. Do not repeat or summarise its content. "
              "Then reply with exactly: DONE")
    for called, is_error, text in parse_stream(_run(prompt, [name, "ToolSearch"], blocked, timeout)):
        if called == name:
            if is_error:
                raise ConnectorError(f"{tool} failed: {text[:300]}")
            try:
                return _unpersist(text)        # a large result arrives as a preview plus a saved file
            except GranolaError as exc:
                raise ConnectorError(f"{tool}: {exc}".replace("Granola", "calendar")) from exc
    raise ConnectorError(f"{tool} was not called; the connector may need re-authorising")
