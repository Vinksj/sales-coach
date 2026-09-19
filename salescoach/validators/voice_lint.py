"""Voice lint for outbound email, from config/style.md.

'block' issues stop approval until fixed. 'warn' issues are shown beside the
draft. autofix() only performs changes that cannot alter meaning.
"""
import re
from dataclasses import dataclass

BANNED = [
    "leverage", "seamless", "robust", "transformative", "revolutionary", "unlock", "harness",
    "journey", "landscape", "testament", "pivotal", "delve", "foster", "empower", "streamline",
    "game-changer", "game changer", "cutting-edge", "next-generation", "ai-powered", "serves as",
    "stands as", "furthermore", "moreover", "consequently", "exciting times",
]

SCHEDULING_BURDEN = [
    "let me know your availability", "when works for you", "what works for you",
    "let's find a time", "lets find a time", "when are you free", "what time works",
    "let me know when you're free", "let me know a time", "share your availability",
]


@dataclass
class LintIssue:
    kind: str
    detail: str
    severity: str   # block | warn

    def as_dict(self):
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


def autofix(text: str) -> str:
    text = re.sub(r"\s*[—]\s*", ", ", text)          # em dash
    text = re.sub(r"(?<=\S) -- (?=\S)", ", ", text)        # spaced double hyphen
    text = re.sub(r"(?<=\w)--(?=\w)", ", ", text)
    return text


def lint(subject: str, body: str) -> list[LintIssue]:
    issues = []
    full = f"{subject}\n{body}"
    low = full.lower()
    if "—" in full or "--" in full:
        issues.append(LintIssue("em_dash", "em dash or double hyphen present", "warn"))
    if "[slots]" in low:
        issues.append(LintIssue("slots", "meeting times still need filling from a verified calendar", "block"))
    for placeholder in sorted(set(re.findall(r"\[[^\]\n]{1,80}\]", full))):
        if placeholder.lower() != "[slots]":
            # Template scaffolding the model left in ("[first name]", "[sign-off]") must never go out.
            issues.append(LintIssue("placeholder", f"unfilled placeholder {placeholder}", "block"))
    for phrase in SCHEDULING_BURDEN:
        if phrase in low:
            issues.append(LintIssue("scheduling_burden", f'"{phrase}" puts scheduling on them', "block"))
    for word in BANNED:
        if re.search(rf"\b{re.escape(word)}\b", low):
            issues.append(LintIssue("banned_word", word, "warn"))
    if re.search(r"\bthank you\b", low):
        issues.append(LintIssue("thanks", 'use "Thanks", not "Thank you"', "warn"))
    if not subject.strip():
        issues.append(LintIssue("subject", "empty subject", "block"))
    from .dates import weekday_mismatches
    for note in weekday_mismatches(full):
        issues.append(LintIssue("weekday", note, "block"))
    for para in [p for p in body.split("\n\n") if p.strip()]:
        if len(para) > 420:
            issues.append(LintIssue("long_paragraph", para[:60] + "...", "warn"))
    return issues


def blocking(issues) -> list:
    return [i for i in issues if i.severity == "block"]
