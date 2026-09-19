"""Claude via the Claude Code CLI (`claude -p`), on the seller's subscription.

Isolation (spikes S3, 2026-09-11). A bare `claude -p` loads the global
~/.claude/CLAUDE.md (the chief-of-staff doc) and every enabled plugin's
SessionStart hook (superpowers injects ~4.4k chars) even with --system-prompt
from an empty cwd. Overriding CLAUDE_CONFIG_DIR or HOME logs it out.
`--setting-sources project` is the fix: from a sandbox cwd with no project
settings, the model sees neither CLAUDE.md nor any plugin, and the
subscription login still works. Verified by asking the model what it could
see (CLAUDE_MD=NONE, SUPERPOWERS=no).

Optional: CLAUDE_CODE_OAUTH_TOKEN in secrets.env (from `claude setup-token`)
additionally moves the config dir into the sandbox.

Every call: no tools, no MCP servers (so gmail-multi's stdio startup never
runs), no user settings, no session persistence, no slash commands, prompt on
stdin.
"""
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from .. import config
from .base import LLMResult, ProviderError, RateLimited, SchemaViolation, json_schema_for

# 2026-09-12: the session limit came back as is_error with "You've hit your session limit"; 429s say so too.
_RATE_LIMIT = re.compile(r"session limit|usage limit|rate.?limit|\b429\b|too many requests|limit .*resets", re.I)

class ClaudeCodeProvider:
    name = "claude_code"

    def __init__(self, binary: str = "~/.local/bin/claude", timeout_s: int = 600, **_):
        self.binary = os.path.expanduser(binary)
        self.timeout_s = timeout_s
        self.sandbox = config.RUNTIME_DIR / "llm-sandbox"
        self.sandbox.mkdir(parents=True, exist_ok=True)
        self.token = config.secret("CLAUDE_CODE_OAUTH_TOKEN")

    @property
    def isolation(self) -> str:
        return "clean+token" if self.token else "clean"

    def _env(self) -> dict:
        env = dict(os.environ)
        if self.token:
            cfg_dir = self.sandbox / "claude-config"
            cfg_dir.mkdir(exist_ok=True)
            env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.token
        return env

    def _run(self, system: str, prompt: str, model: str, effort: Optional[str],
             timeout: Optional[int], json_schema: Optional[dict]) -> tuple[dict, int]:
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=self.sandbox, delete=False) as fh:
            fh.write(system)
            sys_path = fh.name
        cmd = [
            self.binary, "-p",
            "--output-format", "json",
            "--setting-sources", "project",
            "--tools", "",
            "--disallowedTools", "Skill,TodoWrite",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence",
            "--disable-slash-commands",
            "--system-prompt-file", sys_path,
            "--model", model,
        ]
        if effort:
            cmd += ["--effort", effort]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema)]
        started = time.monotonic()
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  cwd=self.sandbox, env=self._env(),
                                  timeout=timeout or self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"claude -p timed out after {exc.timeout}s") from exc
        finally:
            Path(sys_path).unlink(missing_ok=True)
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            tail = (proc.stderr or proc.stdout or "")[-500:]
            raise ProviderError(f"claude -p returned non-JSON (exit {proc.returncode}): {tail}") from exc
        if data.get("is_error"):
            message = str(data.get("result"))[:500]
            if _RATE_LIMIT.search(message):
                raise RateLimited(f"claude -p rate limited: {message}")
            raise ProviderError(f"claude -p error: {message}")
        return data, duration_ms

    def extract_structured(self, *, system, prompt, schema, model,
                           effort=None, timeout=None) -> LLMResult:
        data, duration_ms = self._run(system, prompt, model, effort, timeout, json_schema_for(schema))
        raw = data.get("structured_output")
        if raw is None:
            raise SchemaViolation("no structured_output in claude -p result", str(data.get("result", ""))[:2000])
        try:
            output = schema.model_validate(raw)
        except ValidationError as exc:
            raise SchemaViolation(str(exc), json.dumps(raw)[:4000]) from exc
        return LLMResult(output=output, raw_text=json.dumps(raw), provider=self.name,
                         model=_model_used(data, model), duration_ms=duration_ms,
                         cost_usd=data.get("total_cost_usd"), isolation=self.isolation)

    def generate(self, *, system, prompt, model, effort=None, timeout=None) -> str:
        data, _ = self._run(system, prompt, model, effort, timeout, None)
        return str(data.get("result", ""))


def _model_used(data: dict, requested: str) -> str:
    usage = data.get("modelUsage") or {}
    return next(iter(usage), requested)
