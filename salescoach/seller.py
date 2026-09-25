"""The seller: who this install coaches. One source of truth for every name, address,
domain, language and timezone the code or a prompt needs.

The profile is two halves merged (identity = org profile + acting user; plan, Approach 3):
  ORG_FIELDS   what the company sells and to whom: config/seller.yaml (tracked, empty) with the
               install's own seller.yaml over it (config.user_dir()). One per install.
  USER_FIELDS  who is selling: name, addresses, aliases, signature, timezone, languages, role
               title, call context, style guide. For the local user ("local", the only user of a
               SQLite install) they come from the same seller.yaml and style.md, exactly as before;
               for any other user (cloud) from that user's `users` row, carried on the Actor that
               identity.session() set. profile() with no actor in cloud mode raises identity.NoActor.
Nothing in tracked code or prompts names a person or a company: prompts carry {{variables}} and
render() fills them from the merged profile.

render() substitutes {{name}} ONLY. Prompts contain literal single-brace markers ({garbled},
{bleed}, {asr:partial}) that must reach the model untouched, so this is never str.format. An
unknown {{name}} raises: a typo in a prompt must fail loudly, not ship "{{sellr_name}}" to a model.
"""
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, identity

DEFAULT_TIMEZONE = "Asia/Kolkata"
ORG_FIELDS = ("company", "website", "offering", "icp", "buyer_titles", "vocabulary", "own_domains")
# `role` is the seller.yaml key (a job title); the users table calls the same thing role_title.
USER_FIELDS = ("name", "emails", "role", "aliases", "languages", "timezone", "signature", "call_context")
FIELDS = ("name", "emails", "company", "role", "website", "offering", "icp", "buyer_titles", "vocabulary",
          "own_domains", "aliases", "languages", "timezone", "signature", "call_context")
LIST_FIELDS = ("emails", "own_domains", "aliases", "languages")

# Mailbox providers: an address there says nothing about the company someone works for. They can be
# The seller's OWN domain for the calendar (a personal address on an invite), never an INTERNAL one.
FREE_MAIL = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
                       "yahoo.co.in", "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com",
                       "rediffmail.com", "zoho.com", "gmx.com"})

LANGUAGE_NAMES = {
    "en": "English", "hi": "Hindi", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
    "it": "Italian", "nl": "Dutch", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ar": "Arabic",
    "ta": "Tamil", "te": "Telugu", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "kn": "Kannada",
    "ml": "Malayalam", "pa": "Punjabi", "ur": "Urdu", "id": "Indonesian", "tr": "Turkish", "pl": "Polish",
    "ru": "Russian", "he": "Hebrew", "sv": "Swedish", "vi": "Vietnamese", "th": "Thai",
}
# What the recogniser is known to do to a language, and the everyday phrases a buyer uses in it.
# Only languages the coach was actually tuned on have an entry; the rest get the generic wording.
LANGUAGE_LORE = {
    "Hindi": {
        "mix": "Hinglish",
        "garbled": '"Hi chloral cara march", "Salrat weather is little bit tricky"',
        "fine": '"Humko ye data chahiye by Friday"',
        "hedge": "dekhte hain", "buying_signal": "hamare log use karenge", "objection": "abhi nahi",
    },
}


class NotConfigured(RuntimeError):
    """No seller profile yet. Nothing may create a call: every agent prompt would describe nobody."""


NOT_CONFIGURED = ("The seller profile is not set up yet. Open /setup in the coach (or write seller.yaml in your "
                  "settings folder) before starting or importing a call.")
USER_NOT_CONFIGURED = ("Your profile is not set up yet. Open /me/setup in the coach (your name and email "
                       "address) before starting or importing a call.")


class UnknownVariable(KeyError):
    def __str__(self):
        return self.args[0] if self.args else "unknown prompt variable"


# ------------------------------------------------------------------------------------ profile

def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def _list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,\n;]", value)
    elif not isinstance(value, (list, tuple, set)):    # `emails: 5` in a hand-edited file is one value, not a crash
        value = [value]
    return [s for s in (_clean(v) for v in value) if s]


def _fields(raw: dict, keys) -> dict:
    out = {}
    for key in keys:
        out[key] = _list(raw.get(key)) if key in LIST_FIELDS else _clean(raw.get(key))
    if "emails" in out:
        out["emails"] = list(dict.fromkeys(e.lower() for e in out["emails"]))
    if "own_domains" in out:
        out["own_domains"] = list(dict.fromkeys(d.lower().lstrip("@") for d in out["own_domains"]))
    if "signature" in out:
        out["signature"] = str(raw.get("signature") or "").strip("\n")
    return out


def org_profile() -> dict:
    """The ORG half, from seller.yaml: the company, what it sells, to whom, its domains."""
    return _fields(config.load("seller") or {}, ORG_FIELDS)


def user_profile_from_yaml() -> dict:
    """The USER half as seller.yaml + style.md hold it: the local user's profile, and what
    users.ensure_local copies into the local user's row."""
    out = _fields(config.load("seller") or {}, USER_FIELDS)
    out["role_title"] = out["role"]
    out["style"] = config.text("style.md")
    return out


def user_profile(actor=None) -> dict:
    """The USER half of the acting user: seller.yaml / style.md for the local user, the users row
    (as identity.session loaded it) for anyone else. Raises NoActor in cloud mode with no actor."""
    actor = actor or identity.current_actor()
    if actor.profile is None:
        return user_profile_from_yaml()
    raw = dict(actor.profile)
    raw["role"] = raw.get("role_title") or ""
    out = _fields(raw, USER_FIELDS)
    out["role_title"] = out["role"]
    out["style"] = str(raw.get("style") or "") or config.text("style.md")
    return out


def profile() -> dict:
    """Every field, always present, lists as lists, text stripped. Empty means not given. The org's
    fields and the acting user's, merged (plus role_title and style, the user's style guide)."""
    return {**org_profile(), **user_profile()}


def style_guide() -> str:
    """The acting user's email style guide (style.md for the local user), for the drafters."""
    return user_profile()["style"]


def org_configured() -> bool:
    """The org half is enough to coach with: the company and what it sells."""
    p = org_profile()
    return bool(p["company"] and p["offering"])


def user_configured(actor=None) -> bool:
    """The acting user is enough to coach: a name and one address. False (never NoActor) with no actor."""
    actor = actor or identity.current_actor(required=False)
    if actor is None:
        return False
    p = user_profile(actor)
    return bool(p["name"] and p["emails"])


def is_configured() -> bool:
    """Enough to coach with: who they are, one address, the company, what it sells."""
    return org_configured() and user_configured()


def require_configured() -> None:
    if not org_configured():
        raise NotConfigured(NOT_CONFIGURED)
    if not user_configured():
        raise NotConfigured(USER_NOT_CONFIGURED)


def name() -> str:
    return profile()["name"]


def first_name() -> str:
    full = profile()["name"]
    return full.split()[0] if full else ""


def first_name_upper() -> str:
    """For the block labels in prompts ("SET BY ANNA", "WHAT ANNA SENT")."""
    return (first_name() or "the seller").upper()


def company() -> str:
    """An org field: readable with nobody signed in (the login page's chrome asks for it)."""
    return org_profile()["company"]


def emails() -> list[str]:
    return profile()["emails"]


def primary_email() -> str | None:
    found = profile()["emails"]
    return found[0] if found else None


def aliases() -> list[str]:
    """Names a transcript, a calendar invite or a recorder may use for the seller."""
    p = profile()
    found = [p["name"], first_name(), *p["aliases"], *[e.split("@", 1)[0] for e in p["emails"]]]
    return list(dict.fromkeys(a for a in found if a))


def own_domains() -> list[str]:
    """Domains that are the seller's side of any meeting: the listed ones, else those of their addresses."""
    p = profile()
    if p["own_domains"]:
        return p["own_domains"]
    return list(dict.fromkeys(e.split("@", 1)[1] for e in p["emails"] if "@" in e))


def internal_domains() -> set[str]:
    """The seller's COMPANY domains: colleagues, never buyers. A mailbox provider is not a company."""
    return {d for d in own_domains() if d not in FREE_MAIL}


def from_name() -> str | None:
    return profile()["name"] or None


def team_label() -> str:
    return f"{company()} team" if company() else "Our team"


def app_title() -> str:
    return company() or "Sales Coach"


# Stored actor / stage / decision words as a person should read them.
_WORDS = {"ask_user": "ask you", "needs_user": "needs you", "user": "you", "user:ui": "you"}


def legacy_words() -> dict[str, str]:
    """Spellings an OLDER build stored for today's neutral words (migration 5 renamed a decision
    value, a stage and a column that carried a person's name), as old -> new.

    The tracked source never carries such a spelling. An install that still meets one (an old
    backup opened before it is migrated, an agent output replayed from an old run) lists it in a
    private, untracked file, one `old=new` per line: <settings dir>/legacy-words.txt, or in
    SALESCOACH_LEGACY_WORDS (comma-separated). Resolved on every call: tests move the settings dir.
    """
    found: dict[str, str] = {}
    lines = [raw for raw in (os.environ.get("SALESCOACH_LEGACY_WORDS") or "").split(",")]
    try:
        lines += (config.user_dir() / "legacy-words.txt").read_text().splitlines()
    except OSError:
        pass
    for raw in lines:
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        old, new = (part.strip() for part in raw.split("=", 1))
        if old and new and old != new:
            found[old] = new
    return found


def current_value(value):
    """A stored or model-given word in today's spelling: a legacy spelling is translated, anything
    else is returned untouched (a validator or a CHECK constraint still judges it)."""
    if isinstance(value, str):
        return legacy_words().get(value, value)
    return value


def display(value) -> str:
    """A stored actor / stage / decision word as the user should read it. `user:<id>` (approved_by,
    since Phase 3) reads "you" when it is the acting user, and stays an id for anyone else."""
    text = current_value(str(value or ""))
    if text.startswith("user:") and text not in _WORDS:
        actor = identity.current_actor(required=False)
        if actor is not None and text == f"user:{actor.user_id}":
            return "you"
    return _WORDS.get(text, text)


# ----------------------------------------------------------------------------------- timezone

def timezone() -> str:
    return profile()["timezone"] or DEFAULT_TIMEZONE


def zone() -> ZoneInfo:
    try:
        return ZoneInfo(timezone())
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def tz_label(when: datetime | None = None) -> str:
    """The zone's abbreviation as people write it ("IST", "EST", "CET"); an offset where a zone has none."""
    moment = when.astimezone(zone()) if (when is not None and when.tzinfo) else datetime.now(zone())
    label = moment.tzname() or ""
    if not label or label[0] in "+-":
        offset = moment.strftime("%z")
        return f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return label


# ---------------------------------------------------------------------------------- languages

def languages() -> list[str]:
    """Language NAMES, in the order given. Codes are expanded; anything else is kept as written."""
    found = [LANGUAGE_NAMES.get(l.lower(), l[:1].upper() + l[1:]) for l in profile()["languages"]]
    return list(dict.fromkeys(found)) or ["English"]


def _other_languages() -> list[str]:
    return [l for l in languages() if l != "English"]


def _join(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def language_note() -> str:
    """One sentence on the languages of the calls and what the recogniser does to them."""
    langs, others = languages(), _other_languages()
    if not others:
        return "Calls are in English."
    if "English" not in langs:
        return f"Calls are in {_join(others)}, and the speech recogniser often mangles stretches of it."
    if len(others) == 1:
        return f"Calls mix English and {others[0]}, and the speech recogniser often mangles the {others[0]}."
    return (f"Calls mix English, {_join(others)}, and the speech recogniser often mangles "
            "whatever is not English.")


def language_detail() -> str:
    """How a mangled stretch looks, and what must NOT be mistaken for one. Empty for English-only calls."""
    parts = []
    for lang in _other_languages():
        lore = LANGUAGE_LORE.get(lang, {})
        garbled = f" ({lore['garbled']})" if lore.get("garbled") else ""
        mix = f" ({lore['mix']})" if lore.get("mix") else ""
        fine = f" {lore['fine']} is ok." if lore.get("fine") else ""
        parts.append(f"{lang} is often rendered as English-sounding nonsense{garbled}. "
                     f"{lang} written in Latin script{mix} is NOT garbled if it reads as {lang}.{fine}")
    return " ".join(parts)


def voice_language_note() -> str:
    """For words the coach puts in the seller's mouth. Empty for English-only calls."""
    others = _other_languages()
    if not others or "English" not in languages():
        return ""
    mix = LANGUAGE_LORE.get(others[0], {}).get("mix") if len(others) == 1 else None
    if mix:
        return f"{mix} is fine where the calls are in {mix}."
    return f"Mixing English and {_join(others)} is fine where the calls do."


def _example(kind: str) -> str:
    """', "dekhte hain"': one more example in the buyers' other language, ready to sit in a quoted list."""
    found = [LANGUAGE_LORE[l][kind] for l in _other_languages() if LANGUAGE_LORE.get(l, {}).get(kind)]
    return "".join(f', "{phrase}"' for phrase in found)


# ------------------------------------------------------------------------------------ prompts

def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def seller_context() -> str:
    """The paragraph that tells an agent who is selling what to whom."""
    p = profile()
    first, co = first_name() or "The seller", p["company"] or "their company"
    lines = [f"{first} is {p['role']} at {co}." if p["role"] else f"{first} sells for {co}."]
    if p["offering"]:
        lines.append(_sentence(f"What {co} sells: {p['offering']}"))
    if p["icp"]:
        lines.append(_sentence(f"Who they sell to: {p['icp']}"))
    if p["buyer_titles"]:
        lines.append(_sentence(f"The buyers are usually: {p['buyer_titles']}"))
    if p["call_context"]:
        lines.append(_sentence(p["call_context"]))
    return " ".join(lines)


def seller_brief() -> str:
    """The same in one clause, for prompts that only need to know who ME is."""
    p = profile()
    co = p["company"] or "their company"
    who = f"{p['role']} at {co}" if p["role"] else f"sells for {co}"
    what = f", selling {p['offering'].rstrip('.')}" if p["offering"] else ""
    whom = f" to {p['icp'].rstrip('.')}" if (p["offering"] and p["icp"]) else ""
    return f"{who}{what}{whom}"


def variables() -> dict:
    p = profile()
    first = first_name() or "the seller"
    return {
        "seller_name": p["name"] or "the seller",
        "seller_first_name": first,
        "seller_first_name_upper": first.upper(),
        "company": p["company"] or "the seller's company",
        "role": p["role"],
        "website": p["website"],
        "offering": p["offering"],
        "icp": p["icp"],
        "buyer_titles": p["buyer_titles"],
        "call_context": p["call_context"],
        "signature": p["signature"] or p["name"],
        "timezone": timezone(),
        "tz_label": tz_label(),
        "seller_context": seller_context(),
        "seller_brief": seller_brief(),
        "operator_words": f" ({p['vocabulary']})" if p["vocabulary"] else "",
        "language_note": language_note(),
        "language_detail": language_detail(),
        "voice_language_note": voice_language_note(),
        "eg_hedge": _example("hedge"),
        "eg_buying_signal": _example("buying_signal"),
        "eg_objection": _example("objection"),
    }


VARIABLE = re.compile(r"( ?)\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render(text: str, extra: dict | None = None) -> str:
    """Fill {{variables}} from the profile (and `extra`). Single braces are left exactly as they are.

    A variable that is empty for this seller takes the one space before it with it, and the blank
    paragraph it may leave is closed up, so optional guidance disappears without a trace."""
    if not text or "{{" not in text:
        return text
    values = variables()
    if extra:
        values.update(extra)
    emptied = False

    def fill(match):
        nonlocal emptied
        key = match.group(2)
        if key not in values:
            raise UnknownVariable(f"unknown prompt variable {{{{{key}}}}}; known: {', '.join(sorted(values))}")
        value = str(values[key])
        if not value:
            emptied = True
            return ""
        return match.group(1) + value

    out = VARIABLE.sub(fill, text)
    return re.sub(r"\n{3,}", "\n\n", out) if emptied else out


def prompt(path) -> str:
    """A prompt file, rendered for this seller. Every agent's system prompt comes through here."""
    from pathlib import Path
    return render(Path(path).read_text())
