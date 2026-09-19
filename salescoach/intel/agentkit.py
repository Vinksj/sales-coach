"""Intelligence agents: the core Agent with two differences.

  * prompts live in intel/prompts/<name>.md;
  * the model is chosen like every other agent's: a tier in config/models.yaml.
    An `agents:` block in an intel.yaml written before tiers is still honoured for
    an agent models.yaml does not list. A test override (providers.set_override)
    always wins.

Only route() is overridden. The run loop (validation retry, rate-limit
deferral, fallback, agent_runs logging) is the core's, so it cannot drift:
an earlier private copy of it silently ignored rate limits.
"""
import hashlib
from pathlib import Path

from .. import config, providers, seller
from ..agents.base import Agent

PROMPTS = Path(__file__).with_name("prompts")


def route(name: str):
    return providers.route(name, default_spec=(config.load("intel").get("agents") or {}).get(name))


def shas(system: str, prompt: str) -> tuple[str, str]:
    """(prompt_version, input_sha), computed exactly as the core does."""
    return (hashlib.sha256(system.encode()).hexdigest()[:12],
            hashlib.sha256((system + "\n" + prompt).encode()).hexdigest())


class IntelAgent(Agent):
    def system_prompt(self, ctx: dict) -> str:
        return seller.prompt(PROMPTS / f"{self.name}.md")

    def input_refs(self, ctx: dict) -> dict:
        return {k: ctx[k] for k in ("call_id", "deal_id") if ctx.get(k)}

    def route(self):
        return route(self.name)
