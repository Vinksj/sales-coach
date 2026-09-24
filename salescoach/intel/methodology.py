"""The sales methodology, as data.

The deal strategist scores every deal against a list of elements. Which list is a choice an org
makes once (MEDDPICC, MEDDIC, BANT, SPICED, SPIN, Challenger, Sandler, Command of the Message, or
one it writes itself), so nothing in the code names an element: everything asks active().

  * config/methodologies.yaml is the built-in library (tracked).
  * the user's methodology.yaml is {active: <key>, custom: {<key>: <definition>}}, written with
    config.save_user, so it lives with the rest of this install's settings.
  * MEDDPICC is also defined HERE, in code: a missing or broken YAML file, or an overlay that names
    a methodology that no longer exists, falls back to it rather than stopping the pipeline.

Names that stay: the `meddpicc` table, the "meddpicc" key of a stored strategy and the gate field
`meddpicc.status` are the internal name for "methodology elements", whatever the framework. Rows are
keyed <deal>:<element key>; a row whose key is not in the active methodology is simply not read, so
switching hides the old framework's rows (and the seller's own edits on them) and switching back
brings them back with their user_input protection intact. Frameworks share keys for the same concept
(champion, economic_buyer, decision_process, identify_pain ...), so what is known carries over.

Public API (the setup page calls these):
    available() -> list[dict]           every methodology, built-in and custom, as plain data
    get(key) -> Methodology | None
    active() -> Methodology             never raises
    active_key() -> str
    set_active(key) -> Methodology
    switch(conn, key) -> int            set_active + re-run the strategist on every open deal
    validate_definition(d) -> list[str] error messages for a form; [] when valid
    save_custom(definition) -> str      raises InvalidMethodology(errors)
    delete_custom(key) -> None
"""
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import config
from .schemas import MEDDPICC, MEDDPICC_LABELS, RISK_TYPES

log = logging.getLogger("salescoach.intel")

DEFAULT_KEY = "meddpicc"
KINDS = ("qualification", "conversation")
KEY = re.compile(r"[a-z0-9_]+")
MIN_ELEMENTS, MAX_ELEMENTS = 2, 12
# Rule names the strategist already uses for caps that have nothing to do with a methodology.
RESERVED_RULES = ("single_threaded", "no_dated_buyer_commitment")
# How a lens is written in a sentence, where that differs from the tag on a gap.
LENS_NAMES = {"Gap": "Gap Selling"}

_FRAMEWORK_FIELDS = {"key", "name", "lens", "kind", "description", "elements", "gap_order", "health", "coaching"}
_ELEMENT_FIELDS = {"key", "label", "known_when", "partial_when", "questions", "critical", "cap_when_not_known",
                   "cap_rule", "cap_why", "risk_when_unknown"}
_HEALTH_FIELDS = {"min_known", "rubric", "bottleneck_hint"}
_COACHING_FIELDS = {"analyst", "secondary_lenses", "live", "live_weights"}
_COUNT_WORDS = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
                11: "eleven", 12: "twelve"}
_LIMITS = {"name": 60, "lens": 24, "label": 40, "key": 40, "description": 1500, "known_when": 600,
           "partial_when": 600, "question": 240, "cap_why": 200, "rubric": 2000, "bottleneck_hint": 300,
           "analyst": 2000, "live": 1200}


class InvalidMethodology(ValueError):
    """A definition that did not validate. `errors` is the list a form shows."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# ------------------------------------------------------------------------------ the objects

@dataclass(frozen=True)
class Element:
    key: str
    label: str
    known_when: str
    partial_when: str = ""
    questions: tuple = ()
    critical: bool = False
    cap_when_not_known: int | None = None
    cap_rule: str = ""
    cap_why: str = ""
    risk_when_unknown: str | None = None

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "known_when": self.known_when,
                "partial_when": self.partial_when, "questions": list(self.questions), "critical": self.critical,
                "cap_when_not_known": self.cap_when_not_known, "cap_rule": self.cap_rule, "cap_why": self.cap_why,
                "risk_when_unknown": self.risk_when_unknown}


@dataclass(frozen=True)
class Methodology:
    key: str
    name: str
    lens: str
    kind: str
    description: str
    elements: tuple                       # of Element, in display order
    gap_order: tuple                      # every element key, most important gap first
    min_known: dict | None                # {"count", "cap", "rule"} or None
    rubric: str
    bottleneck_hint: str
    analyst: str
    secondary_lenses: tuple
    live: str
    live_weights: dict
    builtin: bool = True

    @property
    def keys(self) -> tuple:
        return tuple(e.key for e in self.elements)

    @property
    def labels(self) -> dict:
        return {e.key: e.label for e in self.elements}

    @property
    def critical(self) -> tuple:
        return tuple(e for e in self.elements if e.critical)

    @property
    def count(self) -> int:
        return len(self.elements)

    @property
    def lenses(self) -> tuple:
        """The lens tags the call analyst may put on a gap: this methodology's first."""
        return (self.lens, *self.secondary_lenses)

    @property
    def health(self) -> dict:
        return {"min_known": dict(self.min_known) if self.min_known else None, "rubric": self.rubric,
                "bottleneck_hint": self.bottleneck_hint}

    @property
    def coaching(self) -> dict:
        return {"analyst": self.analyst, "secondary_lenses": list(self.secondary_lenses), "live": self.live,
                "live_weights": dict(self.live_weights)}

    def element(self, key):
        return next((e for e in self.elements if e.key == key), None)

    def label(self, key) -> str:
        found = self.element(key)
        return found.label if found else label_for(key)

    def gap_rank(self, key) -> int:
        """Where a gap on this element sorts in a prep brief. A key this methodology does not have
        (a brief or a strategy stored under another one) sorts last instead of raising."""
        try:
            return self.gap_order.index(key)
        except ValueError:
            return len(self.gap_order)

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "lens": self.lens, "kind": self.kind,
                "description": self.description, "builtin": self.builtin,
                "elements": [e.as_dict() for e in self.elements], "gap_order": list(self.gap_order),
                "health": self.health, "coaching": self.coaching}


# ------------------------------------------------------------------------------- validation

def _text(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _block(value) -> str:
    """Multi-line text (a rubric): lines kept, edges trimmed."""
    if not isinstance(value, str):
        return ""
    return "\n".join(line.rstrip() for line in value.strip().splitlines())


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_text(errors, where, name, value, required=False, limit_key=None):
    if value is None or value == "":
        if required:
            errors.append(f"{where}: {name} is required")
        return
    if not isinstance(value, str):
        errors.append(f"{where}: {name} must be text")
        return
    if not value.strip() and required:
        errors.append(f"{where}: {name} is required")
    if "{{" in value or "}}" in value:
        errors.append(f"{where}: {name} cannot contain {{{{ or }}}}")
    limit = _LIMITS.get(limit_key or name)
    if limit and len(value) > limit:
        errors.append(f"{where}: {name} is longer than {limit} characters")


def _check_key(errors, where, key, what="key"):
    if not isinstance(key, str) or not key:
        errors.append(f"{where}: {what} is required")
        return False
    if ":" in key:
        errors.append(f"{where}: {what} '{key}' cannot contain a colon")
        return False
    if not KEY.fullmatch(key):
        errors.append(f"{where}: {what} '{key}' may only use lower-case letters, digits and underscores")
        return False
    if len(key) > _LIMITS["key"]:
        errors.append(f"{where}: {what} '{key}' is longer than {_LIMITS['key']} characters")
        return False
    return True


def _check_cap(errors, where, name, value):
    if not _is_int(value) or not 0 <= value <= 100:
        errors.append(f"{where}: {name} must be a whole number from 0 to 100")


def _triggers() -> tuple:
    from ..coach.detectors import TRIGGERS          # late: the coach package is not needed to read a methodology
    return tuple(TRIGGERS)


def validate_definition(d) -> list[str]:
    """Every problem with a methodology definition, in words a form can show. [] means valid.

    `key` is checked when present (save_custom requires it; built-ins get theirs from the YAML mapping)."""
    if not isinstance(d, dict):
        return ["a methodology must be a mapping of fields"]
    errors = []
    for extra in sorted(set(d) - _FRAMEWORK_FIELDS):
        errors.append(f"unknown field '{extra}'")
    if "key" in d:
        _check_key(errors, "methodology", d.get("key"))
    _check_text(errors, "methodology", "name", d.get("name"), required=True)
    _check_text(errors, "methodology", "lens", d.get("lens"))
    if isinstance(d.get("lens"), str) and re.search(r"[/\[\]\n|]", d["lens"]):
        errors.append("methodology: lens cannot contain / [ ] or |")
    if d.get("kind") not in (None, "", *KINDS):
        errors.append(f"methodology: kind must be one of {', '.join(KINDS)}")
    _check_text(errors, "methodology", "description", d.get("description"))

    elements = d.get("elements")
    keys = []
    if not isinstance(elements, list) or not all(isinstance(e, dict) for e in elements):
        errors.append("elements must be a list of elements")
        elements = []
    elif not MIN_ELEMENTS <= len(elements) <= MAX_ELEMENTS:
        errors.append(f"a methodology needs {MIN_ELEMENTS} to {MAX_ELEMENTS} elements; this one has {len(elements)}")
    rules = {}
    for i, e in enumerate(elements, 1):
        key = e.get("key")
        where = f"element {i}" + (f" ({key})" if isinstance(key, str) and key else "")
        for extra in sorted(set(e) - _ELEMENT_FIELDS):
            errors.append(f"{where}: unknown field '{extra}'")
        if _check_key(errors, where, key):
            if key == "health":
                errors.append(f"{where}: 'health' is reserved (it is the id of the deal health record)")
            elif key.startswith("risk"):
                errors.append(f"{where}: a key cannot start with 'risk' (those ids belong to deal risks)")
            elif key in keys:
                errors.append(f"{where}: the key '{key}' is used twice")
            keys.append(key)
        _check_text(errors, where, "label", e.get("label"), required=True)
        _check_text(errors, where, "known_when", e.get("known_when"), required=True)
        _check_text(errors, where, "partial_when", e.get("partial_when"))
        questions = e.get("questions")
        if questions not in (None, []):
            if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
                errors.append(f"{where}: questions must be a list of text")
            else:
                if len(questions) > 6:
                    errors.append(f"{where}: at most 6 questions")
                for q in questions:
                    _check_text(errors, where, "question", q)
        critical = e.get("critical", False)
        if not isinstance(critical, bool):
            errors.append(f"{where}: critical must be true or false")
        cap = e.get("cap_when_not_known")
        if cap is not None:
            _check_cap(errors, where, "cap_when_not_known", cap)
            if critical is not True:
                errors.append(f"{where}: cap_when_not_known only applies to a critical element")
        elif critical is True:
            errors.append(f"{where}: a critical element needs cap_when_not_known (the most deal health can be "
                          "while it is not known)")
        rule = e.get("cap_rule")
        if rule not in (None, ""):
            if _check_key(errors, where, rule, "cap_rule"):
                if rule in RESERVED_RULES:
                    errors.append(f"{where}: cap_rule '{rule}' is reserved")
                elif rule in rules:
                    errors.append(f"{where}: cap_rule '{rule}' is already used by element {rules[rule]}")
                rules[rule] = i
        _check_text(errors, where, "cap_why", e.get("cap_why"))
        risk = e.get("risk_when_unknown")
        if risk not in (None, "") and risk not in RISK_TYPES:
            errors.append(f"{where}: risk_when_unknown must be one of {', '.join(RISK_TYPES)}")

    order = d.get("gap_order")
    if order not in (None, []):
        if not isinstance(order, list) or not all(isinstance(k, str) for k in order):
            errors.append("gap_order must be a list of element keys")
        else:
            for k in order:
                if k not in keys:
                    errors.append(f"gap_order: '{k}' is not an element of this methodology")
            if len(set(order)) != len(order):
                errors.append("gap_order: an element is listed twice")

    health = d.get("health")
    if health is not None and not isinstance(health, dict):
        errors.append("health must be a mapping")
    elif health:
        for extra in sorted(set(health) - _HEALTH_FIELDS):
            errors.append(f"health: unknown field '{extra}'")
        mk = health.get("min_known")
        if mk is not None and not isinstance(mk, dict):
            errors.append("health: min_known must be a mapping of count, cap and rule")
        elif mk:
            for extra in sorted(set(mk) - {"count", "cap", "rule"}):
                errors.append(f"health: min_known has an unknown field '{extra}'")
            count = mk.get("count")
            if not _is_int(count) or count < 1 or (keys and count > len(keys)):
                errors.append("health: min_known.count must be a whole number from 1 to the number of elements")
            _check_cap(errors, "health", "min_known.cap", mk.get("cap"))
            if mk.get("rule") not in (None, ""):
                if _check_key(errors, "health", mk["rule"], "min_known.rule") and \
                        (mk["rule"] in RESERVED_RULES or mk["rule"] in rules):
                    errors.append(f"health: min_known.rule '{mk['rule']}' is already used")
        _check_text(errors, "health", "rubric", health.get("rubric"))
        _check_text(errors, "health", "bottleneck_hint", health.get("bottleneck_hint"))

    coaching = d.get("coaching")
    if coaching is not None and not isinstance(coaching, dict):
        errors.append("coaching must be a mapping")
    elif coaching:
        for extra in sorted(set(coaching) - _COACHING_FIELDS):
            errors.append(f"coaching: unknown field '{extra}'")
        _check_text(errors, "coaching", "analyst", coaching.get("analyst"))
        _check_text(errors, "coaching", "live", coaching.get("live"))
        lenses = coaching.get("secondary_lenses")
        if lenses not in (None, []):
            if not isinstance(lenses, list) or not all(isinstance(x, str) and x.strip() for x in lenses):
                errors.append("coaching: secondary_lenses must be a list of lens names")
            else:
                for x in lenses:
                    _check_text(errors, "coaching", "lens", x)
                    if re.search(r"[/\[\]\n|]", x):
                        errors.append(f"coaching: lens '{x}' cannot contain / [ ] or |")
                if len(lenses) > 6:
                    errors.append("coaching: at most 6 secondary lenses")
        weights = coaching.get("live_weights")
        if weights not in (None, {}):
            if not isinstance(weights, dict):
                errors.append("coaching: live_weights must map a nudge trigger to a weight")
            else:
                known = _triggers()
                for trigger, weight in weights.items():
                    if trigger not in known:
                        errors.append(f"coaching: live_weights has an unknown trigger '{trigger}' "
                                      f"(known: {', '.join(known)})")
                    elif isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 <= weight <= 1:
                        errors.append(f"coaching: the weight of '{trigger}' must be a number from 0 to 1")
    return errors


# ---------------------------------------------------------------------------------- building

def _join(items, last="and") -> str:
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def _default_rubric(elements) -> str:
    critical = [e.label for e in elements if e.critical]
    needs = _join(critical) if critical else "the elements above"
    return "\n".join([
        "- 0 to 24: no real deal yet.",
        f"- 25 to 49: interest, but the buyer has not confirmed {needs}.",
        f"- 50 to 69: the buyer confirmed {needs}, and there is a next step with an owner and a date.",
        "- 70 to 84: most elements are known, and someone on their side has acted for the deal.",
        "- 85 and above: verbal commitment or paper in motion.",
    ])


def _default_bottleneck(elements) -> str:
    critical = [e.label.lower() for e in elements if e.critical]
    if critical:
        return f"usually {_join(critical, 'or')} not yet confirmed, or no urgency"
    return "usually the first element above that is still unknown, or no urgency"


def build(key: str, d: dict, builtin: bool) -> Methodology:
    """A VALID definition as an object, every default filled in."""
    elements = []
    for e in d["elements"]:
        critical = bool(e.get("critical"))
        label = _text(e["label"])
        elements.append(Element(
            key=e["key"], label=label, known_when=_text(e["known_when"]), partial_when=_text(e.get("partial_when")),
            questions=tuple(_text(q) for q in e.get("questions") or [] if _text(q)), critical=critical,
            cap_when_not_known=e.get("cap_when_not_known") if critical else None,
            cap_rule=(e.get("cap_rule") or f"{e['key']}_not_known") if critical else "",
            cap_why=_text(e.get("cap_why")) or f"{label.lower()} is not confirmed by the buyer",
            risk_when_unknown=e.get("risk_when_unknown") or None))
    keys = [e.key for e in elements]
    order = [k for k in d.get("gap_order") or [] if k in keys]
    if not order:
        order = [e.key for e in elements if e.critical]
    order += [k for k in keys if k not in order]
    health = d.get("health") or {}
    mk = health.get("min_known") or None
    if mk:
        mk = {"count": int(mk["count"]), "cap": int(mk["cap"]),
              "rule": mk.get("rule") or f"{key}_known_below_{int(mk['count'])}"}
    coaching = d.get("coaching") or {}
    name = _text(d["name"])
    lens = _text(d.get("lens")) or name[:_LIMITS["lens"]]
    secondary = tuple(dict.fromkeys(x.strip() for x in coaching.get("secondary_lenses") or [] if x.strip() != lens))
    return Methodology(
        key=key, name=name, lens=lens, kind=d.get("kind") or "qualification", description=_text(d.get("description")),
        elements=tuple(elements), gap_order=tuple(order), min_known=mk,
        rubric=_block(health.get("rubric")) or _default_rubric(elements),
        bottleneck_hint=_text(health.get("bottleneck_hint")) or _default_bottleneck(elements),
        analyst=_text(coaching.get("analyst")), secondary_lenses=secondary, live=_text(coaching.get("live")),
        live_weights={k: float(v) for k, v in (coaching.get("live_weights") or {}).items()}, builtin=builtin)


# What the code did before methodologies were data. Kept here so MEDDPICC works with no YAML at all;
# tests/test_methodology.py holds config/methodologies.yaml to the same keys, labels, caps and rules.
_FALLBACK_KNOWN = {
    "metrics": "The buyer stated the business result they expect, or what the problem costs them, in numbers.",
    "economic_buyer": "The buyer named the person who can release the money, and that person has engaged.",
    "decision_criteria": "The buyer said what they will judge the options on and which criteria weigh most.",
    "decision_process": "The buyer described the steps from here to a decision: who reviews, who approves, by when.",
    "paper_process": "The buyer described what happens after the yes: legal, procurement, who signs, how long it takes.",
    "identify_pain": "The buyer described a specific problem, who feels it and what it costs, in their own words.",
    "champion": "A person with influence has acted for the deal and has a personal reason to want it.",
    "competition": "The buyer said what else they are weighing, including building it or doing nothing.",
}
FALLBACK_DEFINITION = {
    "name": "MEDDPICC", "lens": "MEDDPICC", "kind": "qualification",
    "description": "Eight facts that decide a complex B2B deal: Metrics, Economic Buyer, Decision Criteria, Decision "
                   "Process, Paper Process, Identify Pain, Champion and Competition.",
    "elements": [
        {"key": k, "label": MEDDPICC_LABELS[k], "known_when": _FALLBACK_KNOWN[k],
         **({"critical": True, "cap_when_not_known": 60, "cap_rule": "economic_buyer_not_confirmed",
             "cap_why": "the economic buyer is not confirmed and engaged"} if k == "economic_buyer" else {})}
        for k in MEDDPICC],
    "gap_order": ["economic_buyer", "decision_process", "champion", "paper_process", "metrics", "identify_pain",
                  "decision_criteria", "competition"],
    "health": {
        "min_known": {"count": 3, "cap": 50, "rule": "meddpicc_known_below_3"},
        "rubric": "- 0 to 24: no real deal yet.\n"
                  "- 25 to 49: interest without access to the economic buyer or a known process.\n"
                  "- 50 to 69: economic buyer engaged, pain quantified, a next step with an owner and a date.\n"
                  "- 70 to 84: decision process and paper process known, champion proven by action.\n"
                  "- 85 and above: verbal commitment or paper in motion.",
        "bottleneck_hint": "usually access to the economic buyer, an undefined decision process, or no urgency"},
    "coaching": {"analyst": "", "secondary_lenses": ["SPIN", "Challenger", "Gap"], "live": "", "live_weights": {}},
}


def fallback() -> Methodology:
    return build(DEFAULT_KEY, FALLBACK_DEFINITION, builtin=True)


# ----------------------------------------------------------------------------------- loading

def _stamps() -> tuple:
    user = config.user_dir()
    paths = (Path(config.CONFIG_DIR) / "methodologies.yaml", user / "methodologies.yaml", user / "methodology.yaml")
    return tuple((str(p), config._stamp(p)) for p in paths)


def _safe_load(name: str) -> dict:
    try:
        data = config.load(name)
    except Exception as exc:                         # unreadable YAML must not stop the pipeline
        log.warning("%s.yaml could not be read (%s); using the built-in MEDDPICC", name, exc)
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=8)
def _library(stamps: tuple) -> tuple:
    """({key: Methodology}, default key, overlay's active key) for one version of the three files."""
    tracked = _safe_load("methodologies")
    overlay = _safe_load("methodology")
    found = {}
    raw = tracked.get("methodologies")
    for key, d in (raw.items() if isinstance(raw, dict) else ()):
        errors = validate_definition({**d, "key": key}) if isinstance(d, dict) else ["not a mapping"]
        if errors:
            log.warning("built-in methodology %r is invalid and was skipped: %s", key, "; ".join(errors[:3]))
            continue
        found[key] = build(key, d, builtin=True)
    if DEFAULT_KEY not in found:
        found = {DEFAULT_KEY: fallback(), **found}
    custom = overlay.get("custom")
    for key, d in (custom.items() if isinstance(custom, dict) else ()):
        errors = validate_definition({**d, "key": key}) if isinstance(d, dict) else ["not a mapping"]
        if key in found:
            errors.append("the key is a built-in methodology")
        if errors:
            log.warning("custom methodology %r is invalid and was skipped: %s", key, "; ".join(errors[:3]))
            continue
        found[key] = build(key, d, builtin=False)
    default = tracked.get("default") if tracked.get("default") in found else DEFAULT_KEY
    chosen = overlay.get("active")
    if chosen and chosen not in found:
        log.warning("the active methodology %r does not exist (any more); using %s", chosen, default)
    return found, default, chosen


def _cache_clear():
    _library.cache_clear()


def _all() -> tuple:
    return _library(_stamps())


def get(key) -> Methodology | None:
    return _all()[0].get(key)


def active_key() -> str:
    found, default, chosen = _all()
    return chosen if chosen in found else default


def active() -> Methodology:
    """The methodology this install sells with. Never raises: anything wrong falls back to MEDDPICC."""
    try:
        found, default, chosen = _all()
        return found[chosen if chosen in found else default]
    except Exception:
        log.exception("reading the methodology failed; using the built-in MEDDPICC")
        return fallback()


def available() -> list[dict]:
    """Every methodology as plain data, built-ins first (in file order), then custom ones by name.
    Each: key, name, lens, kind, description, builtin, active, elements[...], gap_order, health, coaching."""
    found, _, _ = _all()
    current = active_key()
    items = [m for m in found.values() if m.builtin] + sorted((m for m in found.values() if not m.builtin),
                                                              key=lambda m: m.name.lower())
    return [{**m.as_dict(), "active": m.key == current} for m in items]


def label_for(element_key: str) -> str:
    """A label for ANY element key: the active methodology's, else any known methodology's, else the
    key in words. Stored strategies, briefs and conflicts outlive a switch, so a lookup never raises."""
    key = str(element_key or "")
    try:
        found = _all()[0]
        ordered = [found[active_key()], *found.values()]
    except Exception:
        ordered = [fallback()]
    for m in ordered:
        e = m.element(key)
        if e:
            return e.label
    return key.replace("_", " ").strip().title() or key


# ------------------------------------------------------------------------------------ saving

def _overlay() -> dict:
    data = config.load_user("methodology")
    out = {"active": data.get("active"), "custom": data.get("custom") if isinstance(data.get("custom"), dict) else {}}
    return out


def _save(overlay: dict):
    data = {"active": overlay.get("active") or DEFAULT_KEY, "custom": overlay.get("custom") or {}}
    config.save_user("methodology", data)
    _cache_clear()


def set_active(key: str) -> Methodology:
    """Choose the methodology. It takes effect on the next read everywhere (no restart). To also
    re-assess the open deals against it, call switch()."""
    m = get(key)
    if m is None:
        raise KeyError(f"no methodology called {key!r}; available: {', '.join(_all()[0])}")
    overlay = _overlay()
    overlay["active"] = key
    _save(overlay)
    return get(key)


def _storable(d: dict) -> dict:
    """The definition as it is written to methodology.yaml: no key (it is the mapping key), no blanks."""
    out = {k: v for k, v in d.items() if k != "key" and v not in (None, "", [], {})}
    out["elements"] = [{k: v for k, v in e.items() if v not in (None, "", [], {}, False)} for e in d["elements"]]
    return out


def save_custom(definition: dict) -> str:
    """Create or replace one of the user's own methodologies. Returns its key.
    Raises InvalidMethodology (with .errors, one message per problem) and saves nothing when it is not valid."""
    errors = validate_definition(definition)
    key = definition.get("key") if isinstance(definition, dict) else None
    if isinstance(definition, dict) and "key" not in definition:
        errors.insert(0, "methodology: key is required")
    existing = get(key) if isinstance(key, str) else None
    if existing is not None and existing.builtin:
        errors.append(f"methodology: '{key}' is a built-in methodology; choose another key")
    if errors:
        raise InvalidMethodology(errors)
    overlay = _overlay()
    overlay["custom"][key] = _storable(definition)
    _save(overlay)
    return key


def delete_custom(key: str) -> None:
    """Remove one of the user's own methodologies. The active one cannot be deleted: switch first.
    Rows already stored for its elements stay in the table, unread."""
    overlay = _overlay()
    if key not in overlay["custom"]:
        raise KeyError(f"no custom methodology called {key!r}")
    if active_key() == key:
        raise ValueError(f"'{key}' is the active methodology; switch to another one before deleting it")
    del overlay["custom"][key]
    _save(overlay)


def switch(conn, key: str) -> int:
    """Make `key` the active methodology and queue a strategist run (STRATEGY_REQUESTED) for every open
    deal that has an analysed call. Returns how many runs were queued; the worker does them.

    Nothing is deleted. Rows of the old methodology stay in the `meddpicc` table and are not read while
    their key is not active: they are hidden from the deal page and the prep brief, and the seller's
    own edits on them are left out of the strategist's prompt. Elements whose key both methodologies
    share carry over as they are. Switching back shows the old rows again, user_input protection intact.
    The strategy cache is keyed on the rendered prompt, which names the elements, so no run is served
    from a strategy made under another methodology.

    Accepted gap: deal_health_history gets no marker. The health line of a deal can therefore step at
    a switch because the caps changed, not because the deal did."""
    from ..orchestrator import bus
    from ..schemas.events import Event
    from ..store.stores import now
    from . import history
    set_active(key)
    queued = 0
    stamp = now()
    for row in conn.execute("SELECT node_id FROM deals WHERE status IN ('active','paused') ORDER BY node_id").fetchall():
        deal_id = row["node_id"]
        if history.anchor_call(conn, deal_id) is None:
            continue                                  # nothing analysed yet: the first call will use the new one
        if bus.publish(conn, Event(type="STRATEGY_REQUESTED", entity_id=deal_id,
                                   payload={"reason": "methodology_switch", "methodology": key},
                                   dedupe_key=f"STRATEGY_REQUESTED:{deal_id}:methodology:{key}:{stamp}"),
                       priority=bus.PRIORITY_BACKFILL):     # every deal at once: behind anything a person waits for
            queued += 1
    conn.commit()
    return queued


# ------------------------------------------------------------------------------------ prompts

def _count_phrase(n: int) -> str:
    return "both elements" if n == 2 else f"all {_COUNT_WORDS.get(n, str(n))} elements"


def count_word(n: int) -> str:
    return "two" if n == 2 else _COUNT_WORDS.get(n, str(n))


def elements_block(m: Methodology) -> str:
    """The strategist's instructions for the element list: the keys, then what each one means."""
    lines = [f"Give {_count_phrase(m.count)} exactly once: {', '.join(m.keys)}."]
    if m.kind == "conversation":
        lines.append(f"{m.name} describes how a selling conversation should go, but every element here is a fact "
                     "about the BUYER. Judge it only by what the buyer said on a call. A question the seller asked, "
                     "or a point the seller made, is not evidence that an element is known.")
    lines.append("What each element means:")
    for e in m.elements:
        line = f"- {e.key} ({e.label}). Known when: {e.known_when}"
        if e.partial_when:
            line += f" Partial when: {e.partial_when}"
        if e.questions:
            line += " Typical questions: " + " ".join(f'"{q}"' for q in e.questions[:2])
        lines.append(line)
    return "\n".join(lines)


def cap_facts(m: Methodology) -> str:
    facts = ["one buyer-side voice", *(f"no {e.label.lower()}" for e in m.critical if e.cap_when_not_known is not None),
             "no dated commitment"]
    return ", ".join(facts)


def lens_sentence(m: Methodology) -> str:
    names = [LENS_NAMES.get(x, x) for x in m.lenses]
    return _join(names)


def prompt_vars(m: Methodology | None = None) -> dict:
    """The {{VARIABLES}} the methodology fills in a prompt file (seller.render's `extra`)."""
    m = m or active()
    return {"METHODOLOGY_NAME": m.name, "ELEMENTS": elements_block(m), "BOTTLENECK": m.bottleneck_hint,
            "HEALTH_RUBRIC": m.rubric, "CAP_FACTS": cap_facts(m), "LENSES": lens_sentence(m)}


def render_prompt(path, m: Methodology | None = None) -> str:
    """A prompt file rendered for this seller AND this methodology."""
    from .. import seller
    return seller.render(Path(path).read_text(), extra=prompt_vars(m))


def analyst_block(m: Methodology | None = None) -> str:
    """What the call analyst is told about how this team sells. Empty for a methodology with no
    coaching text (MEDDPICC: the analyst prompt already speaks it)."""
    m = m or active()
    if not m.analyst:
        return ""
    return (f"## How this team sells: {m.name}\n{m.analyst}\n"
            f"For a gap found through the {m.lens} lens, `element` is one of: {', '.join(m.labels.values())}.")


def live_block(m: Methodology | None = None) -> str:
    m = m or active()
    return f"## How this team sells: {m.name}\n{m.live}" if m.live else ""


def grid_columns(n: int) -> int:
    """Columns of the element grid on the deal page, so 4 to 9 cards (and 2 to 12) fall into full rows."""
    return {1: 1, 2: 2, 3: 3, 4: 4, 5: 3, 6: 3, 7: 4, 8: 4, 9: 3, 10: 5, 11: 4, 12: 4}.get(int(n), 4)
