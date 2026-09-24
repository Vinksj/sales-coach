"""Agent base: one responsibility, explicit input, a pydantic output contract.

run() is the only place an agent touches a model:
  * the system prompt is a versioned file (agents/prompts/<name>.md) rendered for
    this install's seller (seller.render fills its {{variables}}); the hash of the
    RENDERED text is the prompt_version recorded with the run and the artifact, so
    editing the seller profile invalidates cached work exactly like editing a prompt;
  * the output must validate against the schema; one retry with the
    validation error, then the run fails and nothing downstream is written;
  * a provider ERROR (not an invalid answer) falls back once to the fallback
    provider from models.yaml, if it is reachable;
  * every attempt is recorded in agent_runs and committed immediately, so the
    "Why?" view can show failed runs too.
"""
import hashlib
import json
import time
from pathlib import Path

from .. import providers, seller
from ..providers.base import ProviderError, RateLimited, SchemaViolation
from ..store.stores import now
from ..store import db

PROMPTS = Path(__file__).with_name("prompts")


class AgentFailed(RuntimeError):
    def __init__(self, message, rate_limited=False):
        super().__init__(message)
        self.rate_limited = rate_limited


class Agent:
    name: str = ""
    schema = None

    def system_prompt(self, ctx: dict) -> str:
        return seller.prompt(PROMPTS / f"{self.name}.md")

    def build_prompt(self, ctx: dict) -> str:
        raise NotImplementedError

    def input_refs(self, ctx: dict) -> dict:
        return {"call_id": ctx.get("call_id")}

    def route(self):
        """(provider, model, effort) for this agent. Plugins override this, never run()."""
        return providers.route(self.name)

    def run(self, conn, ctx: dict):
        system = self.system_prompt(ctx)
        prompt = self.build_prompt(ctx)
        prompt_version = hashlib.sha256(system.encode()).hexdigest()[:12]
        input_sha = hashlib.sha256((system + "\n" + prompt).encode()).hexdigest()
        provider, model, effort = self.route()
        attempts = [(provider, model, effort)]
        last_error = None
        retry_prompt = prompt
        for attempt in range(3):
            prov, mdl, eff = attempts[min(attempt, len(attempts) - 1)]
            run_id = self._start_run(conn, ctx, prompt_version, prov, mdl, input_sha)
            started = time.monotonic()
            try:
                result = prov.extract_structured(system=system, prompt=retry_prompt, schema=self.schema,
                                                 model=mdl, effort=eff)
            except SchemaViolation as exc:
                last_error = exc
                self._finish_run(conn, run_id, "invalid", error=str(exc)[:4000], output=exc.raw,
                                 duration_ms=int((time.monotonic() - started) * 1000))
                if attempt == 0:
                    retry_prompt = (prompt + "\n\n---\nYour previous answer failed validation:\n"
                                    + str(exc)[:3000] + "\nReturn a corrected answer that matches the schema.")
                    continue
                break
            except ProviderError as exc:
                last_error = exc
                self._finish_run(conn, run_id, "error", error=str(exc)[:4000],
                                 duration_ms=int((time.monotonic() - started) * 1000))
                if isinstance(exc, RateLimited):
                    raise AgentFailed(f"{self.name} rate limited: {exc}", rate_limited=True) from exc
                fb = providers.fallback(self.name)
                if fb and len(attempts) == 1 and getattr(fb[0], "available", lambda: True)():
                    attempts.append((fb[0], fb[1], None))
                    continue
                break
            self._finish_run(conn, run_id, "ok", output=result.raw_text, duration_ms=result.duration_ms,
                             cost_usd=result.cost_usd, model=result.model, isolation=result.isolation)
            return result.output, run_id, prompt_version, input_sha
        raise AgentFailed(f"{self.name} failed: {last_error}")

    def _start_run(self, conn, ctx, prompt_version, provider, model, input_sha) -> int:
        cur = conn.execute(
            "INSERT INTO agent_runs(agent,call_id,prompt_version,provider,model,input_refs,input_sha,status,started_at) "
            "VALUES (?,?,?,?,?,?,?, 'running', ?)",
            (self.name, ctx.get("call_id"), prompt_version, getattr(provider, "name", "?"), model,
             json.dumps(self.input_refs(ctx)), input_sha, now()))
        conn.commit()
        return db.insert_id(cur)

    def _finish_run(self, conn, run_id, status, output=None, error=None, duration_ms=None,
                    cost_usd=None, model=None, isolation=None):
        conn.execute(
            "UPDATE agent_runs SET status=?, output=?, error=?, duration_ms=?, cost_usd=?, "
            "model=COALESCE(?, model), isolation=COALESCE(?, isolation) WHERE id=?",
            (status, output, error, duration_ms, cost_usd, model, isolation, run_id))
        conn.commit()


def record_items(conn, run_id: int, created=None, rejected=None):
    """Attach what a run's output turned into (or why parts were rejected)."""
    conn.execute("UPDATE agent_runs SET created_items=?, rejected_items=? WHERE id=?",
                 (json.dumps(created or []), json.dumps(rejected or []), run_id))
