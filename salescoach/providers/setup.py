"""What the Setup screen calls to choose a model provider. None of these raise for a
provider-side problem: list_models raises ProviderError only, test_connection never raises.

API keys are not handled here. Setup stores a key with config.set_secret(<api_key_env>, ...)
and everything below reads it back through config.secret(); save_choice refuses to write
anything that is, or looks like it could be, a secret.
"""
import time

import httpx
import yaml
from pydantic import BaseModel

from .. import config
from .base import ProviderError
from .http_chat import _KEY_SHAPED

MAX_MODEL_ID = 120

# What Setup may change per provider. api_key_env is fixed per provider on purpose: an
# editable one would let a form post point the bearer token at any environment variable.
EDITABLE = {"base_url", "structured", "timeout_s", "max_tokens", "binary", "endpoint", "num_ctx"}

CATALOG = [
    {"key": "claude_code", "label": "Claude Code (your Claude subscription)", "needs_key": False,
     "api_key_env": None, "base_url": None, "base_url_editable": False,
     "notes": "Runs models through the `claude` CLI on this machine. No API key; the CLI must be "
              "installed and logged in. Model names are the CLI's aliases (opus, sonnet, haiku)."},
    {"key": "anthropic", "label": "Anthropic API", "needs_key": True,
     "api_key_env": "ANTHROPIC_API_KEY", "base_url": "https://api.anthropic.com", "base_url_editable": False,
     "notes": "Claude with your own API key."},
    {"key": "openai", "label": "OpenAI (GPT)", "needs_key": True,
     "api_key_env": "OPENAI_API_KEY", "base_url": "https://api.openai.com/v1", "base_url_editable": False,
     "notes": "No models are preselected: list them and pick one per tier."},
    {"key": "xai", "label": "xAI (Grok)", "needs_key": True,
     "api_key_env": "XAI_API_KEY", "base_url": "https://api.x.ai/v1", "base_url_editable": False,
     "notes": "No models are preselected: list them and pick one per tier."},
    {"key": "openai_compatible", "label": "Any OpenAI-compatible endpoint", "needs_key": False,
     "api_key_env": "LLM_API_KEY", "base_url": "", "base_url_editable": True,
     "notes": "Any server that speaks Chat Completions (a gateway, vLLM, LM Studio). The key is optional "
              "for a local server. https is required unless the endpoint is on this machine or network."},
    {"key": "ollama", "label": "Ollama (local models)", "needs_key": False,
     "api_key_env": None, "base_url": None, "base_url_editable": False,
     "notes": "Runs on this machine. Small local models time out on long call transcripts."},
]


class _Ping(BaseModel):
    reply: str


def check_model_id(model) -> str:
    """A model id as typed or picked. What looks like an API key (sk-..., xai-..., gsk-...), or is far
    longer than any model id, is refused before it can be sent, stored, or shown back: the message
    never repeats the value."""
    text = str(model or "").strip()
    if not text:
        return text
    if _KEY_SHAPED.search(text) or len(text) > MAX_MODEL_ID or any(ch.isspace() for ch in text):
        raise ProviderError("that is not a model id (it looks like an API key, or is too long); paste the key "
                            "into the API key field and pick a model from the list")
    return text


def _descriptor(provider_key: str) -> dict:
    found = next((d for d in CATALOG if d["key"] == provider_key), None)
    if found is None:
        raise ProviderError(f"unknown provider {provider_key!r}")
    return found


def catalog() -> list[dict]:
    """One descriptor per provider Setup can offer, with its current state. `key_set` says
    whether a key is stored; the key itself is never returned."""
    from . import active, claude_cli_available, provider_config
    current, out = active(), []
    for d in CATALOG:
        pcfg = provider_config(d["key"])
        env = pcfg.get("api_key_env") or d["api_key_env"]
        out.append({**d, "api_key_env": env,
                    "base_url": pcfg.get("base_url", d["base_url"]),
                    "tiers": {t: pcfg["tiers"].get(t) or "" for t in ("heavy", "light")},
                    "key_set": bool(env) and config.has_secret(env),
                    "active": d["key"] == current,
                    "available": claude_cli_available() if d["key"] == "claude_code" else True})
    return out


def list_models(provider_key: str, cfg: dict | None = None, *, transport=None) -> list[str]:
    """Model ids the provider offers, sorted. `cfg` holds unsaved values from the form
    (base_url). Raises ProviderError and nothing else."""
    from . import build
    try:
        _descriptor(provider_key)
        if provider_key == "claude_code":
            return ["haiku", "opus", "sonnet"]
        provider = build(provider_key, _clean(provider_key, cfg), transport=transport)
        if provider_key == "ollama":
            try:
                resp = httpx.get(f"{provider.endpoint}/api/tags", timeout=5, follow_redirects=False)
                resp.raise_for_status()
                return sorted(m["name"] for m in resp.json().get("models") or [])
            except (httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, TypeError):
                raise ProviderError("could not list models from Ollama; is it running?") from None
        return provider.list_models()
    except ProviderError:
        raise
    except Exception as exc:                          # the UI gets a sentence, never a traceback
        raise ProviderError(f"could not list models ({type(exc).__name__})") from None


def test_connection(provider_key: str, cfg: dict | None = None, *, timeout: int = 30, transport=None) -> dict:
    """One tiny structured call against the light tier (or cfg['model']).
    Always returns {ok, model, latency_ms, error}; never raises."""
    from . import build, model_for, no_model_message
    out = {"ok": False, "model": None, "latency_ms": None, "error": None}
    started = time.monotonic()
    try:
        _descriptor(provider_key)
        overrides = _clean(provider_key, cfg)
        model, tier = model_for(provider_key, {"tier": "light", "model": check_model_id((cfg or {}).get("model"))},
                                overrides)
        if not model:
            raise ProviderError(no_model_message(provider_key, tier))
        out["model"] = model
        result = build(provider_key, overrides, transport=transport).extract_structured(
            system="This is a connection test.", prompt='Reply with the single word "ok".',
            schema=_Ping, model=model, effort="low", timeout=timeout)
        out.update(ok=True, model=result.model)
    except ProviderError as exc:
        out["error"] = str(exc)[:500]
    except Exception as exc:
        out["error"] = f"unexpected {type(exc).__name__}"
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    return out


test_connection.__test__ = False                      # not a pytest test, whatever imports it


def save_choice(provider_key: str, cfg: dict | None = None, tiers: dict | None = None) -> dict:
    """Make `provider_key` the active provider and store its settings and tier models in the
    user overlay (config.save_user("models", ...)). Returns what was written.
    Raises ValueError for input it will not store."""
    try:
        _descriptor(provider_key)
        clean = _clean(provider_key, cfg)
    except ProviderError as exc:
        raise ValueError(str(exc)) from None
    chosen = {}
    for tier, model in (tiers or {}).items():
        if tier not in ("heavy", "light"):
            raise ValueError(f"unknown tier {tier!r}")
        if not isinstance(model, str) or len(model) > MAX_MODEL_ID:
            raise ValueError(f"the model id for the {tier} tier is not valid")
        try:
            chosen[tier] = check_model_id(model)
        except ProviderError as exc:
            raise ValueError(str(exc)) from None
    overlay = config.load_user("models")               # a file that cannot be read is set aside by save_user
    overlay["provider"] = provider_key
    block = overlay.setdefault("providers", {}).setdefault(provider_key, {})
    form_tiers = clean.pop("tiers", {})
    block.update(clean)
    block.setdefault("tiers", {}).update({**form_tiers, **chosen})
    dumped = yaml.safe_dump(overlay)
    for d in CATALOG:                                 # belt and braces: a pasted key must not land in a file
        value = config.secret(d["api_key_env"]) if d["api_key_env"] else None
        if value and value in dumped:
            raise ValueError("refusing to save: a setting contains an API key")
    config.save_user("models", overlay)
    return overlay


def _clean(provider_key: str, cfg: dict | None) -> dict:
    """The editable, non-secret part of a form's values."""
    from .http_chat import check_base_url
    d = _descriptor(provider_key)
    out = {k: v for k, v in (cfg or {}).items() if k in EDITABLE and v not in (None, "")}
    if "base_url" in out:
        if not d["base_url_editable"]:
            out.pop("base_url")
        else:
            out["base_url"] = check_base_url(str(out["base_url"]), d["label"])
    if isinstance((cfg or {}).get("tiers"), dict):
        out["tiers"] = {t: check_model_id(m) for t, m in cfg["tiers"].items() if t in ("heavy", "light") and m}
    return out
