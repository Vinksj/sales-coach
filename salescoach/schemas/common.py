"""Shared types for agent contracts.

Every agent output model forbids extra keys, so the JSON schema handed to the
model says additionalProperties=false and a drifting output fails validation
instead of being half-read.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict

Confidence = Literal["explicit", "high", "medium", "low"]
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2, "explicit": 3}


def conf_at_least(value: str, floor: str) -> bool:
    return CONFIDENCE_RANK[value] >= CONFIDENCE_RANK[floor]


def conf_min(a: str, b: str) -> str:
    return a if CONFIDENCE_RANK[a] <= CONFIDENCE_RANK[b] else b


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
