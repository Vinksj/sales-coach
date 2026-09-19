"""Agent contracts for sales intelligence.

Same rules as salescoach/schemas: every model forbids extra keys, so a drifting
answer fails validation instead of being half-read. Evidence always names the
call and the turn indexes; quotes are checked against those turns by the code,
never trusted.

Numbers the code already knows (pattern frequencies, trends, health caps) are
deliberately absent from the outputs: the model refers to things by tag or id
and the code attaches the numbers.
"""
from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, create_model

from ..schemas.common import Confidence, Strict

# MEDDPICC is the default methodology and the code-level fallback (intel/methodology.py). Nothing
# outside methodology.py and strategy_model() should read these two constants: the elements a deal
# is scored on are methodology.active().keys.
MEDDPICC = ("metrics", "economic_buyer", "decision_criteria", "decision_process", "paper_process",
            "identify_pain", "champion", "competition")
MEDDPICC_LABELS = {"metrics": "Metrics", "economic_buyer": "Economic Buyer", "decision_criteria": "Decision Criteria",
                   "decision_process": "Decision Process", "paper_process": "Paper Process",
                   "identify_pain": "Identify Pain", "champion": "Champion", "competition": "Competition"}
RISK_TYPES = ("single_threading", "weak_champion", "no_economic_buyer_access", "undefined_decision_process",
              "weak_urgency", "status_quo", "competition", "procurement", "technical", "political")
SUBJECTS = ("momentum", "urgency", "champion_strength", "buying_intent")
LEVELS = ("none", "weak", "moderate", "strong", "unknown")

MeddpiccElement = Literal["metrics", "economic_buyer", "decision_criteria", "decision_process", "paper_process",
                          "identify_pain", "champion", "competition"]
RiskType = Literal["single_threading", "weak_champion", "no_economic_buyer_access", "undefined_decision_process",
                   "weak_urgency", "status_quo", "competition", "procurement", "technical", "political"]
Subject = Literal["momentum", "urgency", "champion_strength", "buying_intent"]
Level = Literal["none", "weak", "moderate", "strong", "unknown"]


class Evidence(Strict):
    call_id: str = Field(description="A call id from the deal history")
    turns: list[int] = Field(description="Turn indexes in THAT call")
    quote: str = Field(description="Verbatim words from those turns")


# ---- Deal Strategist -----------------------------------------------------------

class StakeholderRead(Strict):
    person_id: Optional[str] = Field(
        description="An id from STAKEHOLDERS ON RECORD, OTHER PEOPLE AT THIS ACCOUNT or CONTACTS AT THIS ACCOUNT; "
                    "null only for a person not listed anywhere")
    name: Optional[str] = Field(description="Full name; null only when the person is known by role alone, e.g. the CFO")
    email: Optional[str]
    role: str = Field(description="Title and role in this deal, e.g. 'CFO, economic buyer'")
    influence: Literal["high", "medium", "low", "unknown"]
    incentives: list[str] = Field(description="What this person gains if the deal happens; short phrases")
    concerns: list[str] = Field(description="What worries this person about it; short phrases")
    relationship_strength: Literal["strong", "moderate", "weak", "none", "unknown"] = Field(
        description="The seller's own direct relationship with this person")
    position: Literal["champion", "supporter", "neutral", "skeptic", "blocker", "unknown"]
    champion_potential: Literal["high", "medium", "low", "none", "unknown"]
    ability_to_block: Literal["high", "medium", "low", "unknown"]
    evidence: list[Evidence]
    confidence: Confidence


class MeddpiccRead(Strict):
    element: MeddpiccElement
    status: Literal["known", "partial", "unknown"] = Field(
        description="known only when the buyer confirmed it on a call; partial when some of it was said")
    what_we_know: str
    gap: str = Field(description="What is still missing; empty only when nothing is")
    next_question: str = Field(description="The exact question to ask next, in the seller's words")
    evidence: list[Evidence]
    confidence: Confidence


class RiskRead(Strict):
    type: RiskType
    severity: Literal["critical", "high", "medium", "low"]
    description: str
    evidence: list[Evidence]
    mitigation: str = Field(description="A concrete action that reduces this risk")


class StrategistAssessment(Strict):
    subject: Subject
    level: Level
    stance: str = Field(description="One line, e.g. 'interest high, urgency unproven'")
    confidence: Confidence
    rationale: str
    evidence: list[Evidence]


class NextBestAction(Strict):
    action: str = Field(description="ONE concrete action, imperative, under 25 words")
    why_highest_leverage: str = Field(description="Why this beats the other things the seller could do next")
    owner: Literal["me", "prospect", "mutual"]
    owner_name: Optional[str]
    by_when: Optional[str] = Field(description="ISO date YYYY-MM-DD")
    expected_effect: str
    what_would_change_it: str = Field(description="The fact that, if learned, would change this recommendation")
    evidence: list[Evidence]


class HealthRead(Strict):
    score: int = Field(ge=0, le=100)
    label: str
    rationale: str
    confidence: Confidence


class DealStrategy(Strict):
    summary: str = Field(description="Two or three sentences: where this deal really stands")
    stakeholders: list[StakeholderRead]
    meddpicc: list[MeddpiccRead] = Field(description="All eight elements, each exactly once")
    risks: list[RiskRead]
    assessments: list[StrategistAssessment] = Field(description="momentum, urgency, champion_strength, buying_intent")
    next_best_action: NextBestAction
    deal_health: HealthRead


_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
                11: "eleven", 12: "twelve"}


@lru_cache(maxsize=32)
def strategy_model(keys: tuple = MEDDPICC):
    """The strategist's output contract for a methodology whose element keys are `keys`.

    Only two things depend on the methodology: the `element` enum (the one place it is enforced: an
    element of another framework fails validation and the agent retries) and the "each exactly once"
    description. Both classes keep their names: FakeProvider keys its responses on "DealStrategy" and
    the JSON schema's $defs on "MeddpiccRead" ("meddpicc" is the internal name for methodology elements).
    For MEDDPICC this returns the static DealStrategy itself."""
    keys = tuple(keys)
    if keys == MEDDPICC:
        return DealStrategy
    if len(keys) < 2 or len(set(keys)) != len(keys):
        raise ValueError("a methodology needs at least two distinct element keys")
    read = create_model("MeddpiccRead", __base__=MeddpiccRead, element=(Literal[keys], ...))
    count = _COUNT_WORDS.get(len(keys), str(len(keys)))
    return create_model("DealStrategy", __base__=DealStrategy,
                        meddpicc=(list[read], Field(description=f"All {count} elements, each exactly once")))


# ---- Reconciliation ------------------------------------------------------------

class SubjectVerdict(Strict):
    pair_id: str = Field(description="The pair id given in the input")
    disagree: bool = Field(description="true when the two stances are materially different readings of the deal")
    changed_by_new_evidence: bool = Field(
        description="true when the later stance differs because something new happened, not because the reading differs")
    level: Level = Field(description="The reconciled level")
    verdict: str = Field(description="The reconciled stance, one line")
    rationale: str = Field(description="Why, naming what each side got right or wrong")


class AssessmentReconciliation(Strict):
    verdicts: list[SubjectVerdict]


# ---- Longitudinal coach --------------------------------------------------------

class CallRef(Strict):
    call_id: str
    turns: list[int]
    quote: str = Field(description="Verbatim words from those turns, or empty")


class PatternNote(Strict):
    tag: str = Field(description="A tag from SELLER PATTERNS")
    summary: str = Field(description="What the seller does, in plain words; no numbers")
    evidence: list[CallRef]


class WellHandled(Strict):
    call_id: str
    why: str
    evidence: list[CallRef]


class SayDifferently(Strict):
    situation: str = Field(description="The recurring moment, e.g. 'when the buyer offers to set up a senior meeting'")
    instead_of: CallRef = Field(description="What the seller actually said, quoted")
    say: str = Field(description="What to say next time, short, in the seller's voice")


class CoachReport(Strict):
    headline: str = Field(description="One sentence: how the seller sells right now")
    strengths: list[PatternNote]
    weaknesses: list[PatternNote]
    trajectory: str = Field(description="Explain the given trajectory label using the given trend data; no new numbers")
    priority_tag: str = Field(description="The one weakness to work on now: a tag from SELLER PATTERNS")
    priority_why: str
    practice: str = Field(description="One concrete thing to do on the next call")
    well_handled: list[WellHandled]
    say_differently: list[SayDifferently]


# ---- Prep writer ---------------------------------------------------------------

class PrepWriting(Strict):
    objective: str = Field(description="The one outcome this call must produce, concrete, with owner and date if possible")
    objective_why: str
    opening: str = Field(description="What the seller says in the first minute, in their own voice, 2 to 4 short sentences")
    close: str = Field(description="How the seller closes: names owner, action and date, and asks for it")
    watch_for: list[str] = Field(description="At most three things to listen for on this call")
