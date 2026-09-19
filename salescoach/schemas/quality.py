from typing import Literal

from pydantic import Field

from .common import Strict


class TurnQualityFlag(Strict):
    idx: int
    quality: Literal["partial", "garbled"]
    note: str = Field(description="What is wrong, e.g. 'another language rendered as English nonsense'")


class QualityReport(Strict):
    overall_score: float = Field(ge=0, le=1, description="Share of the call a reader can follow reliably")
    lang_mix: Literal["english", "hinglish", "hindi", "other_mix"]
    flagged_turns: list[TurnQualityFlag] = Field(
        description="Only turns that are partial or garbled. Unlisted turns are ok.")
    cannot_judge: list[str] = Field(
        description="Aspects of this call that cannot be judged reliably from this transcript")
    summary: str
