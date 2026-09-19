"""Provider registry: config/models.yaml names the active provider and gives every agent a tier.

    provider: anthropic
    providers: {anthropic: {api_key_env: ..., tiers: {heavy: <model id>, light: <model id>}}, ...}
    agents:    {call_analyst: {tier: heavy, effort: medium}, ...}

An agent asks for a tier, the active provider says which model that is, so changing
provider is one setting and no agent entry mentions a vendor. LOCAL / CLOUD / HYBRID
is still a config change: an agent entry may pin `provider:` and/or `model:` (an
explicit model beats the tier), which is also what files written before tiers look like.
"""
import json
import os
import shutil

from .. import config
from .base import LLMProvider, ProviderError

TIERS = ("heavy", "light")
DEFAULT_TIER = "light"          # an agent models.yaml does not list (a plugin's) is a light one
# Used when a provider block has no `tiers` (a models.yaml from before tiers). OpenAI and xAI
# deliberately have none: Setup lists the endpoint's models and the user picks.
DEFAULT_TIERS = {
    "claude_code": {"heavy": "opus", "light": "sonnet"},
    "anthropic": {"heavy": "claude-opus-5", "light": "claude-sonnet-5"},
}
LABELS = {"claude_code": "Claude Code", "anthropic": "Anthropic", "openai": "OpenAI", "xai": "xAI",
          "openai_compatible": "the OpenAI-compatible endpoint", "ollama": "Ollama"}

_OVERRIDE = {}
_INSTANCES = {}


def set_override(provider):
    """Force every agent onto one provider (tests, offline dev)."""
    _OVERRIDE["provider"] = provider


def clear_override():
    _OVERRIDE.pop("provider", None)


class Unconfigured:
    """Stands in for a provider that cannot be used as configured. It fails on use, not in
    route(), so the refusal is recorded like any other provider error (agent_runs, the
    live coach's error line) and no request is ever sent."""

    def __init__(self, name: str, message: str):
        self.name = name
        self.message = message

    def available(self) -> bool:
        return False

    def extract_structured(self, **_):
        raise ProviderError(self.message)

    def generate(self, **_):
        raise ProviderError(self.message)


def active() -> str:
    cfg = config.load("models")
    return cfg.get("provider") or cfg.get("default_provider") or "claude_code"


def provider_config(key: str, overrides: dict | None = None) -> dict:
    """The provider's settings: `providers.<key>`, over a legacy top-level `<key>:` block,
    with `overrides` (unsaved values from Setup) on top. `tiers` always present."""
    cfg = config.load("models")
    legacy = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
    block = (cfg.get("providers") or {}).get(key) or {}
    out = {**legacy, **block}
    tiers = dict(out.get("tiers") or DEFAULT_TIERS.get(key) or {})
    for name, value in (overrides or {}).items():
        if name == "tiers":
            tiers.update(value or {})
        else:
            out[name] = value
    out["tiers"] = tiers
    return out


def build(key: str, overrides: dict | None = None, transport=None) -> LLMProvider:
    """A provider instance. HTTP providers are kept per configuration, because they remember
    what an endpoint refused (json_schema, forced tool use) and must not relearn it per call."""
    pcfg = provider_config(key, overrides)
    options = {k: v for k, v in pcfg.items() if k != "tiers"}
    if key == "claude_code":
        from .claude_code import ClaudeCodeProvider
        return ClaudeCodeProvider(**options)
    if key == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider(**options)
    if key != "anthropic" and "base_url" not in options:
        raise ProviderError(f"unknown provider {key!r} in models.yaml")
    cache_key = json.dumps([key, options], sort_keys=True, default=str)
    if transport is None and cache_key in _INSTANCES:
        return _INSTANCES[cache_key]
    from .http_chat import AnthropicProvider, OpenAICompatProvider
    if key == "anthropic":
        provider = AnthropicProvider(transport=transport, **options)
    else:
        options.setdefault("key_required", key != "openai_compatible")
        provider = OpenAICompatProvider(name=key, label=LABELS.get(key, key), transport=transport, **options)
    if transport is None:
        _INSTANCES[cache_key] = provider
    return provider


def model_for(key: str, spec: dict, overrides: dict | None = None) -> tuple[str | None, str]:
    """(model id or None, tier) for an agent entry under a provider."""
    tier = spec.get("tier") or DEFAULT_TIER
    return spec.get("model") or provider_config(key, overrides)["tiers"].get(tier) or None, tier


def no_model_message(key: str, tier: str) -> str:
    return f"no model chosen for the {tier} tier of {key}; pick one in Setup"


def route(agent: str, default_spec: dict | None = None) -> tuple[LLMProvider, str, str | None]:
    """(provider, model, effort) for an agent. `default_spec` is used only when models.yaml
    has no entry for the agent."""
    cfg = config.load("models")
    spec = (cfg.get("agents") or {}).get(agent) or default_spec or {}
    key = spec.get("provider") or active()
    model, tier = model_for(key, spec)
    if "provider" in _OVERRIDE:
        return _OVERRIDE["provider"], model or "default", spec.get("effort")
    if not model:
        return Unconfigured(key, no_model_message(key, tier)), "", spec.get("effort")
    try:
        return build(key), model, spec.get("effort")
    except ProviderError as exc:
        return Unconfigured(key, str(exc)), model, spec.get("effort")


def fallback(agent: str | None = None) -> tuple[LLMProvider, str] | None:
    """The fallback provider, only for agents that opt in with `fallback: true`.

    2026-09-12: a rate-limited backfill fell back to qwen2.5:14b, which timed out
    on every 20k-token transcript. A local model is no substitute for the heavy
    agents, so falling back is opt-in per agent.
    """
    cfg = config.load("models")
    fb = cfg.get("fallback")
    spec = ((cfg.get("agents") or {}).get(agent) or {}) if agent else {}
    if not fb or "provider" in _OVERRIDE or not spec.get("fallback"):
        return None
    return build(fb["provider"]), fb["model"]


def claude_cli_binary() -> str:
    return os.path.expanduser(provider_config("claude_code").get("binary") or "~/.local/bin/claude")


def claude_cli_available() -> bool:
    """Is the `claude` CLI installed? Granola and Calendar read through it whatever the
    model provider is, so without it they are shown as unavailable."""
    binary = claude_cli_binary()
    return (os.path.isfile(binary) and os.access(binary, os.X_OK)) or shutil.which(binary) is not None


from .setup import catalog, list_models, save_choice, test_connection  # noqa: E402,F401  (re-exported; setup needs the names above)
