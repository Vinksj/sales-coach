"""Confidence gates for external actions (spec §19): nothing leaves the Mac
on the strength of a low-confidence inference."""
from .. import config
from ..schemas.common import conf_at_least


def min_external_confidence() -> str:
    return (config.load("policy").get("external_actions") or {}).get("min_confidence", "medium")


def can_feed_external(confidence: str, review_state: str = "proposed", source: str | None = None) -> bool:
    if review_state == "confirmed":
        return True                 # The seller confirmed it; their word is the provenance
    if review_state == "rejected":
        return False
    if source == "recommended":
        return False                # the system's own idea, not something said on the call
    return conf_at_least(confidence, min_external_confidence())
