"""Local models through Ollama's /api/chat with a JSON-schema `format`.

Loopback only, so this does not go through lib/safefetch (which blocks
loopback by design; it exists for third-party hosts).
"""
import json
import time

import httpx
from pydantic import ValidationError

from .base import LLMResult, ProviderError, SchemaViolation, json_schema_for


class OllamaProvider:
    name = "ollama"

    def __init__(self, endpoint: str = "http://localhost:11434", num_ctx: int = 32768,
                 timeout_s: int = 900, **_):
        self.endpoint = endpoint.rstrip("/")
        self.num_ctx = num_ctx
        self.timeout_s = timeout_s

    def available(self) -> bool:
        try:
            return httpx.get(f"{self.endpoint}/api/tags", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def _chat(self, system, prompt, model, fmt, timeout) -> tuple[str, int]:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": self.num_ctx},
        }
        if fmt is not None:
            body["format"] = fmt
        started = time.monotonic()
        try:
            resp = httpx.post(f"{self.endpoint}/api/chat", json=body, timeout=timeout or self.timeout_s)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama request failed: {exc}") from exc
        return resp.json()["message"]["content"], int((time.monotonic() - started) * 1000)

    def extract_structured(self, *, system, prompt, schema, model, effort=None, timeout=None) -> LLMResult:
        text, duration_ms = self._chat(system, prompt, model, json_schema_for(schema), timeout)
        try:
            output = schema.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SchemaViolation(str(exc), text[:4000]) from exc
        return LLMResult(output=output, raw_text=text, provider=self.name, model=model,
                         duration_ms=duration_ms, cost_usd=0.0, isolation="n/a")

    def generate(self, *, system, prompt, model, effort=None, timeout=None) -> str:
        return self._chat(system, prompt, model, None, timeout)[0]
