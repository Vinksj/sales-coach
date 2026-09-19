"""Workflow event contract. Every event is persisted in wf_events before it is handled."""
import uuid
from typing import Optional

from pydantic import BaseModel, Field

KNOWN_EVENT_TYPES = (
    # live
    "CALL_STARTED", "CALL_AUDIO_STREAMING", "NEW_TRANSCRIPT_SEGMENT", "IMPORTANT_SIGNAL_DETECTED",
    "CALL_ENDED",
    # post-call
    "TRANSCRIPT_FINALIZED", "POST_CALL_ANALYSIS_COMPLETE", "ACTION_ITEM_CREATED",
    "REVIEW_COMPLETED", "EMAIL_DRAFT_CREATED", "EMAIL_APPROVED", "EMAIL_SENT", "DEAL_UPDATED",
    # monitoring (phase 4)
    "FOLLOW_UP_DUE", "STAKEHOLDER_REPLY_RECEIVED", "NEXT_CALL_SCHEDULED",
    # internal: re-run the post-call pipeline from a given step
    "PROCESS_CALL",
)
# A plain string so plugins can add event types; the core ones are listed above.
EventType = str


class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: EventType
    entity_id: Optional[str] = None
    occurred_at: Optional[str] = None
    causation_id: Optional[str] = None
    dedupe_key: Optional[str] = None
    payload: dict = Field(default_factory=dict)
