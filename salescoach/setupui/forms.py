"""Form text -> validated settings, with one message per field so the page can put it beside the input.

Nothing here writes anything. Two forms need real parsing:
  * the seller profile (step 1), including the style guide that becomes the user's style.md
  * the custom methodology builder (step 2), whose rules live in methodology.validate_definition;
    this module only builds the definition from the rows and maps the engine's messages back to
    the field that caused them.
"""
import re
from functools import lru_cache
from urllib.parse import urlparse
from zoneinfo import available_timezones

from .. import seller

# Single-line inputs. A browser never puts a line break in one, so a CR or LF here is a crafted
# request; the name goes into the From header of every email, so it is refused, not tidied away.
SINGLE_LINE = {"name": ("Your name", 80), "role": ("Your role", 80), "company": ("Company", 80),
               "website": ("Website", 120), "buyer_titles": ("Who the buyers usually are", 300),
               "vocabulary": ("Words your buyers use", 300), "timezone": ("Timezone", 64)}
LIST_INPUTS = {"emails": "Your email addresses", "own_domains": "Company email domains",
               "aliases": "Other names", "languages": "Languages"}
# Textareas. The first two are one paragraph each (line breaks are folded into spaces).
PARAGRAPHS = {"offering": ("What you sell", 600), "icp": ("Who you sell to", 400)}
BLOCKS = {"call_context": ("How you sell", 1500), "signature": ("Email sign-off", 600)}
STYLE_LIMIT = 20000
REQUIRED = {"name": "Your name is needed.", "company": "Your company's name is needed.",
            "offering": "One sentence on what you sell is needed."}

EMAIL = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
DOMAIN = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
LANGUAGE = re.compile(r"[A-Za-z][A-Za-z \-]{0,29}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@lru_cache(maxsize=1)
def timezones() -> tuple:
    """IANA zone names for the timezone picker: Area/City names plus UTC, sorted."""
    names = {z for z in available_timezones()
             if "/" in z and not z.startswith(("posix/", "right/", "Etc/", "SystemV/"))}
    return tuple(sorted(names | {"UTC"}))


def _newlines(value: str) -> str:
    return (value or "").replace("\r\n", "\n")


def _domain(value: str) -> str:
    """'https://www.acme.com/' and '@acme.com' are what people paste; the domain is what is meant."""
    text = value.strip().lower().lstrip("@")
    if "://" in text:
        text = urlparse(text).netloc or text
    return text.split("/", 1)[0]


def parse_profile(form: dict) -> tuple[dict, dict]:
    """(data for seller.yaml, {field: message}). `form` holds the raw strings as posted."""
    data, errors = {}, {}

    def fail(field, message):
        errors.setdefault(field, message)

    for field, (label, limit) in SINGLE_LINE.items():
        raw = form.get(field) or ""
        if "\r" in raw or "\n" in raw:
            fail(field, f"{label} cannot contain a line break.")
            raw = " ".join(raw.split())
        data[field] = " ".join(raw.split())
        if len(data[field]) > limit:
            fail(field, f"{label} is too long (over {limit} characters).")
    for field, (label, limit) in PARAGRAPHS.items():
        data[field] = " ".join((form.get(field) or "").split())
        if len(data[field]) > limit:
            fail(field, f"{label} is too long (over {limit} characters).")
    for field, (label, limit) in BLOCKS.items():
        text = _newlines(form.get(field) or "")
        if "\r" in text:
            fail(field, f"{label} contains a stray carriage return; please retype it.")
            text = text.replace("\r", "\n")
        data[field] = text.strip("\n") if field == "signature" else text.strip()
        if len(data[field]) > limit:
            fail(field, f"{label} is too long (over {limit} characters).")
    for field, label in LIST_INPUTS.items():
        raw = form.get(field) or ""
        if "\r" in raw or "\n" in raw:
            fail(field, f"{label} cannot contain a line break; separate them with commas.")
        data[field] = seller._list(raw)

    data["emails"] = list(dict.fromkeys(e.lower() for e in data["emails"]))
    data["own_domains"] = list(dict.fromkeys(_domain(d) for d in data["own_domains"]))
    data["languages"] = data["languages"] or ["en"]

    for field, message in REQUIRED.items():
        if not data[field]:
            fail(field, message)
    if not data["emails"]:
        fail("emails", "At least one email address is needed.")
    bad = [e for e in data["emails"] if not EMAIL.fullmatch(e)]
    if bad:
        fail("emails", f"{bad[0]} does not look like an email address.")
    bad = [d for d in data["own_domains"] if not DOMAIN.fullmatch(d)]
    if bad:
        fail("own_domains", f"{bad[0]} does not look like an email domain (try company.com).")
    if data["website"] and (" " in data["website"] or "." not in data["website"]):
        fail("website", f"{data['website']} does not look like a website (try www.company.com).")
    if data["timezone"] and data["timezone"] not in available_timezones():
        fail("timezone", f"{data['timezone']} is not a timezone name (try Asia/Kolkata or America/New_York).")
    bad = [l for l in data["languages"] if not LANGUAGE.fullmatch(l)]
    if bad:
        fail("languages", f"{bad[0]} does not look like a language (try en, hi, es, or the full name).")
    if len(data["languages"]) > 6:
        fail("languages", "List at most six languages, the main one first.")
    if any(len(a) > 60 for a in data["aliases"]) or len(data["aliases"]) > 10:
        fail("aliases", "At most ten other names, each under 60 characters.")
    if len(data["emails"]) > 10:
        fail("emails", "At most ten addresses.")

    for field, value in data.items():
        text = "\n".join(value) if isinstance(value, list) else str(value)
        if _CONTROL.search(text):
            fail(field, "This field contains a character that cannot be stored. Please retype it.")
        if "{{" in text or "}}" in text:
            fail(field, "Please do not use {{ or }} here.")
    return data, errors


def parse_style(text: str) -> tuple[str, str | None]:
    """(style guide text, error). The drafters render the guide through seller.render, where an unknown
    {{variable}} raises: refuse it here rather than break every draft later."""
    text = _newlines(text or "").replace("\r", "\n").strip()
    if len(text) > STYLE_LIMIT:
        return text, f"The style guide is too long (over {STYLE_LIMIT} characters)."
    if _CONTROL.search(text):
        return text, "The style guide contains a character that cannot be stored. Please retype that part."
    try:
        seller.render(text)
    except seller.UnknownVariable as exc:
        name = re.search(r"\{\{(\w+)\}\}", str(exc))
        return text, (f"The style guide uses {{{{{name.group(1) if name else '...'}}}}}, which the coach does not know. "
                      "Remove it, or use one of {{seller_first_name}}, {{company}}, {{signature}}.")
    return (text + "\n") if text else "", None


def same_text(a: str, b: str) -> bool:
    def norm(t):
        return "\n".join(line.rstrip() for line in _newlines(t or "").strip().splitlines())
    return norm(a) == norm(b)


# ------------------------------------------------------------------------ methodology builder

MAX_ROWS, MIN_ROWS = 12, 3
_KEEP_FRAMEWORK = ("lens", "gap_order", "health", "coaching")
_KEEP_ELEMENT = ("cap_rule", "cap_why", "risk_when_unknown")


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:limit].strip("_")


def _at(values, i) -> str:
    return (values[i] if i < len(values) else "") or ""


def rows_from_definition(definition: dict | None) -> list[dict]:
    rows = []
    for e in (definition or {}).get("elements") or []:
        cap = e.get("cap_when_not_known")
        rows.append({"key": e.get("key") or "", "label": e.get("label") or "", "known_when": e.get("known_when") or "",
                     "partial_when": e.get("partial_when") or "", "questions": "\n".join(e.get("questions") or []),
                     "critical": bool(e.get("critical")), "cap": "" if cap is None else str(cap)})
    return pad_rows(rows)


def pad_rows(rows: list[dict], extra: int = 0) -> list[dict]:
    want = min(MAX_ROWS, max(MIN_ROWS, len(rows) + extra))
    blank = {"key": "", "label": "", "known_when": "", "partial_when": "", "questions": "", "critical": False, "cap": ""}
    return rows + [dict(blank) for _ in range(want - len(rows))]


def parse_methodology(form: dict, existing: dict | None = None, fixed_key: str | None = None):
    """(definition for methodology.save_custom, rows as typed, row_map).

    `form` values: name, key, kind, description (str); el_key, el_label, el_known_when, el_partial_when,
    el_questions, el_cap (parallel lists, one entry per row); el_critical (list of checked row numbers).
    `existing` is the stored definition when editing: what this form does not show (lens, health,
    coaching, an element's cap rule) is carried over. row_map[i] is the row that element i+1 came from.
    """
    labels = list(form.get("el_label") or [])[:MAX_ROWS]
    checked = {str(v) for v in form.get("el_critical") or []}
    old = {e.get("key"): e for e in (existing or {}).get("elements") or [] if isinstance(e, dict)}
    rows, elements, row_map = [], [], []
    for i in range(len(labels)):
        row = {"key": _at(form.get("el_key") or [], i).strip(), "label": " ".join(labels[i].split()),
               "known_when": " ".join(_at(form.get("el_known_when") or [], i).split()),
               "partial_when": " ".join(_at(form.get("el_partial_when") or [], i).split()),
               "questions": _newlines(_at(form.get("el_questions") or [], i)).strip(),
               "critical": str(i) in checked, "cap": _at(form.get("el_cap") or [], i).strip()}
        rows.append(row)
        if not (row["label"] or row["known_when"] or row["partial_when"] or row["questions"]):
            continue
        key = row["key"] or slug(row["label"]) or f"element_{len(elements) + 1}"
        element = {k: v for k, v in (old.get(key) or {}).items() if k in _KEEP_ELEMENT}
        element.update(key=key, label=row["label"], known_when=row["known_when"], critical=row["critical"])
        if row["partial_when"]:
            element["partial_when"] = row["partial_when"]
        questions = [" ".join(q.split()) for q in row["questions"].splitlines() if q.strip()]
        if questions:
            element["questions"] = questions
        if row["critical"]:
            if row["cap"]:                         # left empty: the engine asks for it, in its own words
                element["cap_when_not_known"] = int(row["cap"]) if re.fullmatch(r"\d{1,3}", row["cap"]) else row["cap"]
        else:
            element.pop("cap_rule", None)
        elements.append(element)
        row_map.append(i)

    name = " ".join((form.get("name") or "").split())
    definition = {k: v for k, v in (existing or {}).items() if k in _KEEP_FRAMEWORK}
    keys = [e["key"] for e in elements]
    if definition.get("gap_order"):
        definition["gap_order"] = [k for k in definition["gap_order"] if k in keys]
    definition.update(key=fixed_key or (form.get("key") or "").strip().lower() or slug(name), name=name,
                      kind=(form.get("kind") or "qualification").strip(),
                      description=" ".join((form.get("description") or "").split()), elements=elements)
    return definition, pad_rows(rows), row_map


_WORDS = (("a critical element", "a deal-breaker"), ("critical", "deal-breaker"), ("cap_when_not_known", "a health cap"), ("known_when", "“Known when”"),
          ("partial_when", "“Partly known when”"))


# The engine speaks of keys; this form never shows one for an element (it is made from the name).
_KEY_TALK = ((re.compile(r"a key cannot start with 'risk'.*", re.S),
              "The name cannot start with “risk”: the coach keeps that word for deal risks"),
             (re.compile(r"'health' is reserved.*", re.S),
              "“Health” is taken by the deal health score; use another name"),
             (re.compile(r"the key '[^']*' is used twice"), "Two elements have the same name"),
             (re.compile(r"key '([^']*)' may only use.*", re.S),
              r"The short id “\1” may only use lower-case letters, digits and underscores"),
             (re.compile(r"key '([^']*)' cannot contain a colon"), r"The short id “\1” cannot contain a colon"))


def _say(message: str) -> str:
    for pattern, plain in _KEY_TALK:
        message = pattern.sub(plain, message)
    for word, plain in _WORDS:
        message = message.replace(word, plain)
    message = message.strip()
    message = message[:1].upper() + message[1:]
    return message if message.endswith((".", ")", "?")) else message + "."


def map_methodology_errors(errors: list[str], row_map: list[int]) -> tuple[dict, list]:
    """The engine's messages, each beside the input that caused it: ({field id: [messages]}, [the rest]).
    Field ids: name, key, kind, description, elements, el<row>_label|known_when|partial_when|questions|cap."""
    fields, general = {}, []
    for message in errors:
        found = re.match(r"element (\d+)(?: \([^)]*\))?: (.*)", message, re.S)
        if found:
            index, rest = int(found.group(1)) - 1, found.group(2)
            row = row_map[index] if 0 <= index < len(row_map) else index
            if rest.startswith("known_when"):
                part = "known_when"
            elif rest.startswith("partial_when"):
                part = "partial_when"
            elif "question" in rest:
                part = "questions"
            elif "cap_when_not_known" in rest or rest.startswith("critical"):
                part = "cap"
            else:                                    # the label, or the key that is made from it
                part = "label"
            fields.setdefault(f"el{row}_{part}", []).append(_say(rest))
            continue
        target = None
        if message.startswith("methodology: "):
            rest = message[len("methodology: "):]
            target = next((f for f in ("name", "kind", "description") if rest.startswith(f)), None)
            if target is None and (rest.startswith("key") or "built-in" in rest):
                target = "key"
            message = rest
        elif message.startswith(("a methodology needs", "elements must")):
            target = "elements"
        if target:
            fields.setdefault(target, []).append(_say(message))
        else:
            general.append(_say(message))
    return fields, general
