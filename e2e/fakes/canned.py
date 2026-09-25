"""The fake model's answers: one canned, schema-valid object per pipeline agent (keyed by the JSON-schema name
the OpenAI-compatible provider sends, which is the pydantic class name), and a generic generator for any
other schema, so every agent the stack calls gets an answer that validates.

The canned answers cite the e2e meeting (e2e/test_e2e.py MEETING): turn 0 the rep who owns the copy opens,
turn 1 the buyer asks for the plant-wise split, turn 2 a rep commits to send it by Friday, turn 3 the buyer
agrees to set up the CFO meeting. Quotes are verbatim so the evidence gates keep them.
"""
import copy
import re

BUYER_EMAIL = "arjun@northwind.test"

QUALITY = {"overall_score": 0.9, "lang_mix": "english", "flagged_turns": [], "cannot_judge": [],
           "summary": "Clear call."}

SUMMARY = {"what_happened": "Walked Arjun through the pilot; he asked for the plant-wise savings split.",
           "key_discussions": [{"text": "Savings by plant", "evidence_turns": [1]}],
           "decisions": [], "commitments": [], "open_questions": [], "risks": [], "next_step": None}

ANALYSIS = {
    "claims": [{"subject": "deal.economic_buyer", "statement": "CFO wants plant-wise savings", "kind": "fact",
                "confidence": "explicit", "evidence_turns": [1],
                "evidence_quote": "our CFO will want to see the savings split by plant"}],
    "gaps": [{"lens": "MEDDPICC", "element": "Decision Process", "missing": "who signs",
              "why_it_matters": "pilot stalls", "question_to_ask": "Who signs the pilot?"}],
    "seller": [{"dimension": "discovery", "judged": True, "assessment": "ok", "evidence_turns": [0]}],
    "observations": [],
    "assessments": [{"subject": "deal.momentum", "stance": "interest high, urgency unproven",
                     "confidence": "medium", "rationale": "no date", "evidence_turns": [3]}],
    "verdict": {"label": "held", "one_line": "Interest, no commitment", "rationale": "vague CFO meeting"},
    "what_changed": ["CFO named as reviewer"],
    "biggest_missed_opportunity": {"evidence_turns": [3], "what_happened": "accepted 'try to'",
                                   "what_to_do_instead": "pin the date", "suggested_words": "Can we lock a date?"},
    "coaching_insight": {"insight": "Pin dates.", "evidence_turns": [3], "practice_next_call": "Say back owner and date."},
}

ACTIONS = {"actions": [
    {"description": "Send the plant-wise savings breakdown to Arjun", "type": "my_action", "owner": "me",
     "owner_name": None, "source": "explicit_commitment",
     "evidence_quote": "I will send the plant-wise breakdown by Friday", "evidence_turns": [2],
     "confidence": "explicit", "priority": "high", "due_date": None, "due_date_confidence": "unknown",
     "follow_up_required": False, "follow_up_strategy": "", "dependencies": [], "world_commitment_id": None},
], "loop_updates": [], "notes": ""}

EMAIL = {"to": [BUYER_EMAIL], "cc": [], "subject": "Plant-wise savings and the CFO meeting",
         "body": "Hi Arjun,\n\nThanks for the time today.\n\nI'll send the plant-wise breakdown by Friday.\n\nBest",
         "rationale": "short call, short email"}

CANNED = {"QualityReport": QUALITY, "CallSummary": SUMMARY, "CallAnalysis": ANALYSIS, "ActionExtraction": ACTIONS,
          "EmailDraft": EMAIL}


def answer(name: str, schema: dict) -> dict:
    """The canned object for `name`, fitted to `schema` (unknown keys dropped, missing required ones generated);
    for any other schema, a generated minimal instance."""
    defs = schema.get("$defs") or schema.get("definitions") or {}
    canned = CANNED.get(name)
    if canned is None:
        return generate(schema, defs)
    return fit(copy.deepcopy(canned), schema, defs)


def _resolve(node: dict, defs: dict) -> dict:
    while isinstance(node, dict) and "$ref" in node:
        node = defs.get(re.sub(r"^#/(\$defs|definitions)/", "", node["$ref"]), {})
    return node


def fit(value, schema: dict, defs: dict):
    schema = _resolve(schema, defs)
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        props = schema["properties"]
        out = {k: fit(v, props[k], defs) for k, v in value.items() if k in props}
        for key in schema.get("required") or list(props):
            if key not in out:
                out[key] = generate(props[key], defs)
        return out
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [fit(v, schema["items"], defs) for v in value]
    return value


def generate(schema: dict, defs: dict, depth: int = 0):
    schema = _resolve(schema, defs)
    if depth > 12:
        return None
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if schema.get(key):
            options = [_resolve(o, defs) for o in schema[key]]
            if any(o.get("type") == "null" for o in options):
                return None
            return generate(options[0], defs, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        if "null" in kind:
            return None
        kind = kind[0]
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {k: generate(v, defs, depth + 1) for k, v in props.items()
                if k in (schema.get("required") or list(props))}
    if kind == "array":
        n = int(schema.get("minItems") or 0)
        return [generate(schema.get("items") or {}, defs, depth + 1) for _ in range(n)]
    if kind == "string":
        return "e2e" + "x" * max(0, int(schema.get("minLength") or 0) - 3)
    if kind == "integer":
        return int(schema.get("minimum") or 0)
    if kind == "number":
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and high is not None:
            return (low + high) / 2
        return float(low or 0)
    if kind == "boolean":
        return False
    return None
