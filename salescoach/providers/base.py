"""Provider interfaces. No module outside providers/ knows which vendor runs a model."""
import copy
from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    pass


class RateLimited(ProviderError):
    """The subscription or API refused for quota. Waiting is the only fix; never fall back or burn retries."""


class SchemaViolation(ProviderError):
    """The model answered, but the answer does not match the contract."""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


@dataclass
class LLMResult:
    output: BaseModel
    raw_text: str
    provider: str
    model: str
    duration_ms: int
    cost_usd: Optional[float] = None
    isolation: str = "n/a"


class LLMProvider(Protocol):
    name: str

    def extract_structured(self, *, system: str, prompt: str, schema: type[T], model: str,
                           effort: Optional[str] = None, timeout: Optional[int] = None) -> LLMResult: ...

    def generate(self, *, system: str, prompt: str, model: str,
                 effort: Optional[str] = None, timeout: Optional[int] = None) -> str: ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class Segment:
    """One transcribed stretch of a single channel."""
    channel: str                  # me | them
    t_start: float
    t_end: float
    text: str
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None
    words: list = field(default_factory=list)


class SpeechProvider(Protocol):
    def transcribe_file(self, path: str, channel: str, lang_mode: str) -> list[Segment]: ...


class StreamingSpeechProvider(Protocol):
    def open(self, channel: str, lang_mode: str) -> "SpeechStream": ...


class SpeechStream(Protocol):
    def feed(self, pcm16: bytes, t_offset: float) -> Iterable[Segment]: ...
    def close(self) -> Iterable[Segment]: ...


class EmailProvider(Protocol):
    def send(self, message) -> dict: ...
    def save_draft(self, message) -> dict: ...
    def read_replies(self, thread_id: str, since: str) -> list[dict]: ...


def json_schema_for(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with every $ref inlined.

    Inlining keeps the schema self-contained for every provider (CLI flag,
    Ollama `format`), whatever their $ref support. Our contracts are not
    recursive, so inlining terminates.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                merged = copy.deepcopy(defs[name])
                extra = {k: v for k, v in node.items() if k != "$ref"}
                merged.update(extra)
                return resolve(merged)
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)
