"""Bring-your-own-key chat providers over plain HTTPS.

  AnthropicProvider      Messages API. Structured output is a forced tool call whose
                         input_schema is the agent's contract.
  OpenAICompatProvider   Chat Completions: OpenAI, xAI (Grok), and any endpoint that
                         speaks the same protocol. Structured output is
                         response_format json_schema (strict); an endpoint that
                         rejects it gets json_object with the schema in the prompt.

Rules both follow:
  * the API key is read from config.secret() at call time and lives only in the
    request headers: never on the instance, never in an exception, a log line,
    a repr or an LLMResult. Exceptions are raised `from None` so the httpx
    request (which carries the headers) is not chained onto them;
  * every request has an explicit timeout (the caller's, else timeout_s) and
    follows no redirects;
  * the provider validates the answer with pydantic itself: what comes back is
    an LLMResult or one of RateLimited / SchemaViolation / ProviderError.

lib/safefetch is not used here: it is GET-only and blocks loopback, and a local
OpenAI-compatible server (LM Studio, vLLM) is a legitimate endpoint.
"""
import ipaddress
import json
import re
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .. import config
from .base import LLMResult, ProviderError, RateLimited, SchemaViolation, json_schema_for

ANTHROPIC_VERSION = "2023-06-01"
CONNECT_TIMEOUT_S = 10.0
RESULT_TOOL = "record_result"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_KEY_SHAPED = re.compile(r"\b(?:sk|xai|gsk)-[A-Za-z0-9_\-*\.]{6,}")
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.S)


class RequestRejected(ProviderError):
    """A 4xx that is about the request itself (not auth, not quota). Carries the status
    and the API's own message so a provider can degrade once and retry."""

    def __init__(self, message: str, status: int, detail: str):
        super().__init__(message)
        self.status = status
        self.detail = detail


def strict_schema(schema: dict) -> dict:
    """The schema as OpenAI-style strict mode wants it: no `default`, every property
    required, additionalProperties false on every object. A nullable field stays
    anyOf[..., null], so "optional" becomes "required, may be null"."""
    def fix(node):
        if isinstance(node, list):
            return [fix(v) for v in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key == "default":
                continue
            if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
                out[key] = {name: fix(sub) for name, sub in value.items()}   # names are not keywords
            else:
                out[key] = fix(value)
        if isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out
    return fix(schema)


def check_base_url(url: str, label: str = "the endpoint") -> str:
    """Normalised base URL, or ProviderError. Plain http only for a local endpoint."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ProviderError(f"no base URL set for {label}; add one in Setup")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProviderError(f"the base URL for {label} must look like https://host/v1")
    if parts.username or parts.password:
        raise ProviderError(f"the base URL for {label} must not contain credentials")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ProviderError(f"the base URL for {label} must not contain a query string or a fragment")
    if parts.scheme == "http" and not _is_local(parts.hostname):
        raise ProviderError(f"plain http is only allowed for a local endpoint; use https for {label}")
    return url


def _is_local(host: str) -> bool:
    if host == "localhost" or host.endswith(".local") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _scrub(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "[redacted]")
    return _KEY_SHAPED.sub("[redacted]", text)


def _strip_fences(text: str) -> str:
    match = _FENCE.match(text)
    return (match.group(1) if match else text).strip()


class _HTTPProvider:
    name = ""
    label = ""

    def __init__(self, base_url: str, api_key_env: Optional[str], key_required: bool = True,
                 timeout_s: float = 600, transport=None):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key_env = api_key_env
        self.key_required = key_required
        self.timeout_s = timeout_s
        self._transport = transport

    def __repr__(self) -> str:
        state = "set" if self._key() else "not set"
        return f"<{type(self).__name__} {self.name} {self.base_url or '(no base URL)'} key {self.api_key_env}: {state}>"

    def _key(self) -> str:
        return (config.secret(self.api_key_env) or "").strip() if self.api_key_env else ""

    def available(self) -> bool:
        """Configured well enough to try a request. No network."""
        return bool(self.base_url) and (bool(self._key()) or not self.key_required)

    def _auth_headers(self, key: str) -> dict:
        raise NotImplementedError

    def _request(self, method: str, path: str, *, body: Optional[dict] = None, params: Optional[dict] = None,
                 timeout: Optional[float] = None) -> dict:
        base = check_base_url(self.base_url, self.label)
        key = self._key()
        if self.key_required and not key:
            raise ProviderError(f"no API key set for {self.label} ({self.api_key_env}); add it in Setup")
        seconds = float(timeout or self.timeout_s)
        limits = httpx.Timeout(seconds, connect=min(seconds, CONNECT_TIMEOUT_S))
        try:
            with httpx.Client(transport=self._transport, timeout=limits, follow_redirects=False) as client:
                resp = client.request(method, base + path, json=body, params=params,
                                      headers=self._auth_headers(key))
        except httpx.TimeoutException:
            raise ProviderError(f"{self.label} did not answer within {seconds:g}s") from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            host = urlsplit(base).hostname
            raise ProviderError(f"could not reach {self.label} at {host} ({type(exc).__name__})") from None
        self._raise_for_status(resp, key)
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"{self.label} returned a response that is not JSON") from None
        if not isinstance(data, dict):
            raise ProviderError(f"{self.label} returned an unexpected response shape")
        return data

    def _raise_for_status(self, resp: httpx.Response, key: str):
        status = resp.status_code
        if 200 <= status < 300:
            return
        detail = _scrub(_api_message(resp), key)
        if status == 429:
            wait = resp.headers.get("retry-after")
            raise RateLimited(f"{self.label} rate limited the request (HTTP 429"
                              + (f", retry after {wait}s" if wait and wait.isdigit() else "") + f"): {detail}")
        if status in (401, 403):
            raise ProviderError(f"the API key was rejected by {self.label} (HTTP {status}); "
                                f"check {self.api_key_env} in Setup")
        if 300 <= status < 400:
            raise ProviderError(f"{self.label} answered with a redirect (HTTP {status}); check the base URL")
        if status >= 500:
            raise ProviderError(f"{self.label} is having trouble (HTTP {status}); try again shortly")
        if status == 404:
            raise ProviderError(f"{self.label} did not find that (HTTP 404); check the model id and the "
                                f"base URL: {detail}")
        raise RequestRejected(f"{self.label} rejected the request (HTTP {status}): {detail}", status, detail)

    def _result(self, schema, raw, text: str, model: str, started: float) -> LLMResult:
        try:
            output = schema.model_validate(raw)
        except ValidationError as exc:
            raise SchemaViolation(str(exc), text[:4000]) from None
        return LLMResult(output=output, raw_text=text, provider=self.name, model=model,
                         duration_ms=int((time.monotonic() - started) * 1000), cost_usd=None, isolation="n/a")


def _api_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return "(no detail)"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        err = err.get("message")
    if not isinstance(err, str):
        err = data.get("message") if isinstance(data, dict) else None
    return (err if isinstance(err, str) and err else "(no detail)")[:300]


class AnthropicProvider(_HTTPProvider):
    """Claude through the Messages API with the org's own key.

    `effort` is advisory: it is sent as output_config.effort (it is what keeps a live-coach
    pass inside its 40 s), and a model that rejects it is remembered and asked without.
    Likewise a model that rejects forced tool use (Claude Fable 5.1) is asked again with
    tool_choice auto and an instruction to call the tool.
    """
    name = "anthropic"
    label = "Anthropic"

    def __init__(self, api_key_env: str = "ANTHROPIC_API_KEY", base_url: str = "https://api.anthropic.com",
                 max_tokens: int = 16000, timeout_s: float = 600, transport=None, **_):
        super().__init__(base_url, api_key_env, True, timeout_s, transport)
        self.max_tokens = int(max_tokens)      # room for the ~9 KB DealStrategy contract plus thinking
        self._auto_tool = set()                # models that refuse a forced tool_choice
        self._no_effort = set()                # models that refuse output_config.effort

    def _auth_headers(self, key):
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}

    def _messages(self, system, prompt, model, effort, timeout, tool: Optional[dict]) -> dict:
        for _ in range(3):                     # at most one retry per thing a model can refuse
            body = {"model": model, "max_tokens": self.max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            if system:
                body["system"] = system
            if effort in EFFORTS and model not in self._no_effort:
                body["output_config"] = {"effort": effort}
            if tool is not None:
                body["tools"] = [tool]
                if model in self._auto_tool:
                    body["tool_choice"] = {"type": "auto"}
                    body["messages"][0]["content"] = (
                        prompt + f"\n\nAnswer by calling the `{RESULT_TOOL}` tool exactly once. Do not answer in text.")
                else:
                    body["tool_choice"] = {"type": "tool", "name": RESULT_TOOL}
            try:
                return self._request("POST", "/v1/messages", body=body, timeout=timeout)
            except RequestRejected as exc:
                if exc.status == 400 and "tool_choice" in exc.detail and "tool_choice" in body \
                        and model not in self._auto_tool:
                    self._auto_tool.add(model)
                elif exc.status == 400 and "output_config" in body and re.search(r"effort|output_config", exc.detail):
                    self._no_effort.add(model)
                else:
                    raise
        raise ProviderError(f"{self.label} kept rejecting the request for {model}")

    def extract_structured(self, *, system, prompt, schema, model, effort=None, timeout=None) -> LLMResult:
        started = time.monotonic()
        tool = {"name": RESULT_TOOL, "description": f"Record the {schema.__name__}. Call this exactly once.",
                "input_schema": json_schema_for(schema)}
        data = self._messages(system, prompt, model, effort, timeout, tool)
        blocks = [b for b in data.get("content") or [] if isinstance(b, dict)]
        if data.get("stop_reason") == "refusal":
            raise ProviderError(f"{data.get('model') or model} declined the request (safety refusal)")
        call = next((b for b in blocks if b.get("type") == "tool_use" and b.get("name") == RESULT_TOOL), None)
        if call is None:
            said = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            raise SchemaViolation("the model answered without calling the result tool", said[:4000])
        text = json.dumps(call.get("input"), ensure_ascii=False)
        if data.get("stop_reason") == "max_tokens":
            raise SchemaViolation(f"the answer was cut off at max_tokens={self.max_tokens}", text[:4000])
        return self._result(schema, call.get("input"), text, data.get("model") or model, started)

    def generate(self, *, system, prompt, model, effort=None, timeout=None) -> str:
        data = self._messages(system, prompt, model, effort, timeout, None)
        if data.get("stop_reason") == "refusal":
            raise ProviderError(f"{data.get('model') or model} declined the request (safety refusal)")
        return "".join(b.get("text", "") for b in data.get("content") or []
                       if isinstance(b, dict) and b.get("type") == "text")

    def list_models(self, timeout: float = 15) -> list[str]:
        ids, after = [], None
        for _ in range(5):
            params = {"limit": 1000, **({"after_id": after} if after else {})}
            data = self._request("GET", "/v1/models", params=params, timeout=timeout)
            ids += _model_ids(data)
            after = data.get("last_id")
            if not data.get("has_more") or not after:
                break
        return sorted(set(ids))


class OpenAICompatProvider(_HTTPProvider):
    """Chat Completions for OpenAI, xAI and compatible endpoints.

    No max_tokens and no temperature are sent: the parameter names and allowed values
    differ between model families, and every one of them has a usable default.
    `effort` is ignored for the same reason (reasoning_effort is a 400 on most models).
    """

    def __init__(self, base_url: str = "", api_key_env: Optional[str] = "LLM_API_KEY",
                 structured: str = "json_schema", name: str = "openai_compatible", label: Optional[str] = None,
                 key_required: bool = True, timeout_s: float = 600, transport=None, **_):
        if structured not in ("json_schema", "json_object"):
            raise ProviderError(f"structured must be json_schema or json_object, not {structured!r}")
        super().__init__(base_url, api_key_env, key_required, timeout_s, transport)
        self.name = name
        self.label = label or name
        self.structured = structured
        self._json_object = set()              # models whose endpoint refused json_schema

    def _auth_headers(self, key):
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    def _chat(self, system, prompt, model, response_format, timeout) -> tuple[str, str]:
        body = {"model": model, "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": prompt}]}
        if response_format:
            body["response_format"] = response_format
        data = self._request("POST", "/chat/completions", body=body, timeout=timeout)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"{self.label} returned an unexpected response shape") from None
        if message.get("refusal"):
            raise ProviderError(f"{data.get('model') or model} declined the request: {str(message['refusal'])[:200]}")
        content = message.get("content")
        if isinstance(content, list):          # some servers answer in parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = content if isinstance(content, str) else ""
        if choice.get("finish_reason") == "length":
            raise SchemaViolation("the answer was cut off at the model's output limit", text[:4000])
        return text, data.get("model") or model

    def extract_structured(self, *, system, prompt, schema, model, effort=None, timeout=None) -> LLMResult:
        started = time.monotonic()
        full = json_schema_for(schema)
        strict = {"type": "json_schema",
                  "json_schema": {"name": schema.__name__, "strict": True, "schema": strict_schema(full)}}
        loose_system = (system + "\n\nReturn ONLY one JSON object, with no prose and no code fences, that "
                        "validates against this JSON Schema:\n" + json.dumps(full, ensure_ascii=False))
        if self.structured == "json_object" or model in self._json_object:
            text, used = self._chat(loose_system, prompt, model, {"type": "json_object"}, timeout)
        else:
            try:
                text, used = self._chat(system, prompt, model, strict, timeout)
            except RequestRejected as exc:
                if exc.status != 400:
                    raise
                # Remembered only once json_object has worked: a 400 about something else
                # (context length, a bad parameter) fails again here and changes nothing.
                text, used = self._chat(loose_system, prompt, model, {"type": "json_object"}, timeout)
                self._json_object.add(model)
        text = _strip_fences(text)
        if not text:
            raise SchemaViolation("the model returned an empty answer", "")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchemaViolation(f"the answer was not valid JSON: {exc}", text[:4000]) from None
        return self._result(schema, raw, text, used, started)

    def generate(self, *, system, prompt, model, effort=None, timeout=None) -> str:
        return self._chat(system, prompt, model, None, timeout)[0]

    def list_models(self, timeout: float = 15) -> list[str]:
        return sorted(set(_model_ids(self._request("GET", "/models", timeout=timeout))))


def _model_ids(data: dict) -> list[str]:
    rows = data.get("data")
    if not isinstance(rows, list):
        raise ProviderError("the models endpoint returned an unexpected response shape")
    return [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)]
