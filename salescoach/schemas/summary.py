from typing import Literal, Optional

from pydantic import Field

from .common import Strict


class Cited(Strict):
    text: str
    evidence_turns: list[int] = Field(description="Turn indexes that support this item")


class SummaryCommitment(Strict):
    owner: Literal["me", "prospect", "mutual"]
    owner_name: Optional[str]
    text: str
    evidence_turns: list[int]


class CallSummary(Strict):
    what_happened: str = Field(description="Short factual narrative, 2 to 4 sentences")
    key_discussions: list[Cited]
    decisions: list[Cited] = Field(description="Only decisions explicitly made on the call")
    commitments: list[SummaryCommitment]
    open_questions: list[Cited]
    risks: list[Cited]
    next_step: Optional[Cited] = Field(description="The agreed next step, or null if none was agreed")
