"""Deterministic provider for tests and offline development.

Responses are keyed by schema class name. A value may be a dict (validated
against the schema), a callable(system, prompt) -> dict, or a list consumed in
order (to script a bad answer followed by a good one).
"""
import json
from typing import Callable, Union

from pydantic import ValidationError

from .base import LLMResult, SchemaViolation

Response = Union[dict, Callable[[str, str], dict], list]


class FakeProvider:
    name = "fake"

    def __init__(self, responses: dict[str, Response] | None = None):
        self.responses = dict(responses or {})
        self.calls: list[dict] = []

    def _next(self, key, system, prompt):
        value = self.responses.get(key)
        if value is None:
            raise SchemaViolation(f"FakeProvider has no response for {key}")
        if isinstance(value, list):
            if not value:
                raise SchemaViolation(f"FakeProvider exhausted responses for {key}")
            value = value.pop(0)
        return value(system, prompt) if callable(value) else value

    def extract_structured(self, *, system, prompt, schema, model, effort=None, timeout=None) -> LLMResult:
        key = schema.__name__
        self.calls.append({"schema": key, "system": system, "prompt": prompt, "model": model})
        raw = self._next(key, system, prompt)
        try:
            output = schema.model_validate(raw)
        except ValidationError as exc:
            raise SchemaViolation(str(exc), json.dumps(raw)) from exc
        return LLMResult(output=output, raw_text=json.dumps(raw), provider=self.name, model=model,
                         duration_ms=1, cost_usd=0.0, isolation="n/a")

    def generate(self, *, system, prompt, model, effort=None, timeout=None) -> str:
        self.calls.append({"schema": None, "system": system, "prompt": prompt, "model": model})
        value = self._next("__text__", system, prompt)
        return value if isinstance(value, str) else json.dumps(value)
