from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, create_model

from .common import Confidence, Strict

ClaimSubject = Literal[
    "buyer.problem", "buyer.goal", "buyer.priority", "buyer.fear", "buyer.constraint",
    "buyer.urgency", "buyer.buying_signal", "buyer.risk_signal",
    "deal.metrics", "deal.economic_buyer", "deal.decision_criteria", "deal.decision_process",
    "deal.paper_process", "deal.pain", "deal.champion", "deal.competition", "deal.stakeholder",
    "deal.status_quo", "deal.procurement", "deal.progress",
]

SellerDimensionName = Literal[
    "discovery", "listening", "question_quality", "conversation_control", "pitching",
    "objection_handling", "executive_presence", "ability_to_challenge", "next_step_discipline",
]


class Claim(Strict):
    subject: ClaimSubject
    statement: str
    kind: Literal["fact", "inference", "assumption"] = Field(
        description="fact = explicitly stated; inference = reasonably supported; "
                    "assumption = believed but not confirmed")
    confidence: Confidence
    evidence_turns: list[int]
    evidence_quote: str = Field(description="Verbatim words from the cited turns, or empty for an assumption")


DEFAULT_LENSES = ("MEDDPICC", "SPIN", "Challenger", "Gap")


class Gap(Strict):
    lens: Literal["MEDDPICC", "SPIN", "Challenger", "Gap"]
    element: str = Field(description="e.g. Economic Buyer, Implication, Teach, Root cause")
    missing: str
    why_it_matters: str
    question_to_ask: str


class SellerDimension(Strict):
    dimension: SellerDimensionName
    judged: bool = Field(description="false when transcript quality does not allow a fair judgement")
    assessment: str
    evidence_turns: list[int]


class SellerObservation(Strict):
    tag: str = Field(description="A tag from the seller taxonomy, or new:<snake_case> for a new behaviour")
    polarity: Literal["weakness", "strength"]
    severity: Literal["low", "medium", "high"]
    contexts: list[str] = Field(description="e.g. end_of_call, senior_buyer, objection, pricing")
    evidence_turns: list[int]
    evidence_quote: str
    confidence: Confidence


class Assessment(Strict):
    subject: Literal["deal.momentum", "deal.urgency", "deal.champion_strength", "deal.buying_intent"]
    stance: str = Field(description="e.g. 'strong buying signal' or 'interest high, urgency unproven'")
    confidence: Confidence
    rationale: str
    evidence_turns: list[int]


class Verdict(Strict):
    label: Literal["advanced", "held", "stalled", "regressed", "unclear"]
    one_line: str
    rationale: str


class MissedOpportunity(Strict):
    evidence_turns: list[int]
    what_happened: str
    what_to_do_instead: str
    suggested_words: str


class CoachingInsight(Strict):
    insight: str
    evidence_turns: list[int]
    practice_next_call: str


class CallAnalysis(Strict):
    claims: list[Claim]
    gaps: list[Gap]
    seller: list[SellerDimension]
    observations: list[SellerObservation]
    assessments: list[Assessment]
    verdict: Verdict
    what_changed: list[str] = Field(description="What is different about the deal after this call")
    biggest_missed_opportunity: Optional[MissedOpportunity]
    coaching_insight: CoachingInsight


@lru_cache(maxsize=32)
def analysis_model(lenses: tuple = DEFAULT_LENSES, elements: tuple = ()):
    """The call analyst's contract when the team's methodology is not MEDDPICC: `Gap.lens` is the
    active methodology's lens plus its secondary lenses, and `Gap.element` gives that methodology's
    labels as examples. Class names are unchanged (FakeProvider keys on "CallAnalysis"). For the
    default lenses this is the static CallAnalysis itself."""
    lenses = tuple(lenses)
    if lenses == DEFAULT_LENSES:
        return CallAnalysis
    if not lenses or len(set(lenses)) != len(lenses):
        raise ValueError("lenses must be distinct and not empty")
    examples = ", ".join([*elements[:3], "Implication", "Root cause"][:5])
    gap = create_model("Gap", __base__=Gap, lens=(Literal[lenses], ...),
                       element=(str, Field(description=f"The element of that lens, e.g. {examples}")))
    return create_model("CallAnalysis", __base__=CallAnalysis, gaps=(list[gap], ...))
