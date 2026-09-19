"""Action & commitment extraction contract (spec §8)."""
from typing import Literal, Optional

from pydantic import Field

from .common import Confidence, Strict

LoopType = Literal["my_action", "prospect_action", "mutual", "follow_up", "info_request", "deal_risk"]
Owner = Literal["me", "prospect", "mutual", "internal"]
ActionSource = Literal["explicit_commitment", "implied_commitment", "recommended"]
Priority = Literal["critical", "high", "medium", "low"]


class ExtractedAction(Strict):
    description: str
    type: LoopType
    owner: Owner
    owner_name: Optional[str] = Field(description="Named person responsible, if known")
    source: ActionSource = Field(
        description="explicit_commitment only when someone clearly committed on the call; "
                    "recommended when nobody committed but the deal needs it")
    evidence_quote: str = Field(description="Verbatim words from the cited turns; empty only for recommended")
    evidence_turns: list[int]
    confidence: Confidence
    priority: Priority
    due_date: Optional[str] = Field(description="ISO date YYYY-MM-DD or null")
    due_date_confidence: Literal["explicit", "inferred", "unknown"]
    follow_up_required: bool
    follow_up_strategy: str
    dependencies: list[str] = Field(description="Descriptions of other actions this depends on")
    world_commitment_id: Optional[str] = Field(
        default=None,
        description="Id from OTHER OPEN COMMITMENTS when this action is the same promise already tracked there")


class LoopUpdate(Strict):
    loop_id: str
    proposed_status: Literal["done", "cancelled", "superseded", "waiting"]
    reason: str
    evidence_quote: str
    evidence_turns: list[int]
    confidence: Confidence
    superseded_by_action: Optional[int] = Field(
        description="Index into actions[] of the new action that replaces this loop, if superseded")


class ActionExtraction(Strict):
    actions: list[ExtractedAction]
    loop_updates: list[LoopUpdate]
    notes: str
