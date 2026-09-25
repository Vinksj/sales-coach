"""What is set up, what runs on a default and what is missing: computed, every time, from the
settings files, the secrets' presence and the state table. Never from "this page was opened".

Nothing here talks to a service. The connection checks look at files and at what earlier runs
left in the state table; they never call Gmail, the calendar connector or a model.

State keys this package writes (none holds a secret):
  setup:provider_test   JSON {provider, ok, model, latency_ms, error, at}: the last Test connection
  setup:finished_at     when Finish was pressed on the review step
  setup:card_dismissed  what the "Finish setting up" card was dismissed for (card_signature)
"""
import json
import os
from pathlib import Path

from .. import config, hosted, identity, providers, seller, sources
from ..intel import methodology
from ..store.stores import get_state, get_user_state
from . import words

STEPS = (("you", "You and your org"), ("method", "How you sell"), ("model", "Model"),
         ("sources", "Where calls come from"), ("connections", "Email and calendar"), ("review", "Review"))
TITLES = dict(STEPS)
TEST_KEY, FINISHED_KEY, DISMISSED_KEY = "setup:provider_test", "setup:finished_at", "setup:card_dismissed"


def href(step: str) -> str:
    return f"/setup/{step}"


def neighbours(step: str) -> tuple:
    keys = [k for k, _ in STEPS]
    i = keys.index(step)
    return (keys[i - 1] if i else None), (keys[i + 1] if i + 1 < len(keys) else None)


# ------------------------------------------------------------------------------------ provider

def last_test(conn) -> dict | None:
    try:
        found = json.loads(get_state(conn, TEST_KEY) or "null")
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def provider_state(conn) -> dict:
    """The active provider: whether the user chose it, whether it can be used, whether a test passed."""
    catalog = providers.catalog()
    current = next((d for d in catalog if d["active"]), None)
    key = providers.active()
    out = {"key": key, "label": words.provider_name(key, current["label"] if current else key),
           "short": words.provider_name(key, current["label"] if current else key, short=True), "descriptor": current,
           "chosen": bool(config.load_user("models").get("provider")), "usable": False, "why": "",
           "tiers": dict(current["tiers"]) if current else {}, "test": None, "tested_ok": False}
    if current is None:
        out["why"] = "The chosen provider is not one the coach knows."
    elif key == "claude_code" and not current["available"]:
        out["why"] = ("The Claude CLI is not available in a hosted install; choose a provider with an API key."
                      if hosted.is_hosted() else "The Claude CLI is not installed on this machine.")
    elif current["needs_key"] and not current["key_set"]:
        out["why"] = "No API key is stored for it."
    elif current["base_url_editable"] and not current["base_url"]:
        out["why"] = "Its address (base URL) is not filled in."
    elif not all(out["tiers"].get(t) for t in providers.TIERS):
        out["why"] = "No model is chosen for one of the two tiers."
    else:
        out["usable"] = True
    test = last_test(conn)
    if test and test.get("provider") == key:
        out["test"] = test
        out["tested_ok"] = bool(test.get("ok"))
    return out


# ------------------------------------------------------------------------------------- sources

def sources_state(conn) -> dict:
    catalog = sources.catalog(conn)
    saved = [e for e in config.load_user("sources").get("sources") or [] if isinstance(e, dict)]
    extra = [d for d in catalog if d["mode"] != "export" and d["kind"] not in ("upload", "folder")
             and d["enabled"] and d["configured"] and (d["mode"] == "poll" or d["kind"] == "webhook")]
    return {"catalog": catalog, "chosen": bool(saved), "extra": extra,
            "folder_on": any(d["kind"] == "folder" and d["enabled"] for d in catalog)}


def drop_dir() -> Path:
    from ..sources.adapters.folder import FolderAdapter
    return FolderAdapter(sources.settings()["folder"]["options"]).drop_dir().resolve()


# --------------------------------------------------------------------------------- connections

def _gmail_token(alias: str) -> tuple[bool, bool]:
    """(client file present, token file present) for the sending account. Existence only: the token
    is never opened. The registry (config.json: alias -> token file name) is read only when the
    token is not at its usual place."""
    from ..execution import gmail
    folder = Path(gmail.GMAIL_DIR)
    client = (folder / "credentials.json").is_file()
    token = folder / "tokens" / f"{alias}.json"
    if not token.is_file():
        try:
            entry = (json.loads((folder / "config.json").read_text()).get("accounts") or {}).get(alias) or {}
            named = Path(os.path.expanduser(str(entry.get("tokenFile") or "")))
            if entry.get("tokenFile"):
                token = named if named.is_absolute() else folder / named
        except (OSError, ValueError, AttributeError):
            pass
    return client, token.is_file()


def capture_binary() -> Path:
    raw = (config.load("asr").get("capture") or {}).get("binary") or "callcap/build/callcap"
    path = Path(os.path.expanduser(str(raw)))
    return path if path.is_absolute() else Path(config.ROOT) / path


def _speech_models() -> tuple[list, list]:
    """(downloaded, missing) speech-to-text models, from the local cache only."""
    from ..speech import models
    have, missing = [], []
    for repo in models.required_models():
        (have if models.is_downloaded(repo) else missing).append(repo)
    return have, missing


HOSTED = "Not available in a hosted install"


def _hosted_connections() -> list[dict]:
    """The same rows on a container host: nothing on this list can be reached there, and nothing on it
    is something the seller can fix, so every row is optional and says what to do instead."""
    return [
        {"key": "gmail", "title": "Gmail: sending and reading replies", "ok": False, "optional": True, "status": HOSTED,
         "enables": "Sends the follow-up email when you press Send, and reads replies to the emails it sent.",
         "detail": "Gmail needs a sign-in stored on the machine that runs the coach, which a hosted install does not "
                   "have. Drafts are still written: copy each one into your own mail and mark the call as sent.",
         "fix": ""},
        {"key": "calendar", "title": "Calendar", "ok": False, "optional": True, "status": HOSTED,
         "enables": "Lists your meetings on the Today page and fills real free times into follow-up emails.",
         "detail": "The calendar is read through the Claude CLI, which a hosted install does not have. Replace "
                   "[SLOTS] in a draft with times yourself before sending it.",
         "fix": ""},
        {"key": "capture", "title": "Recording calls", "ok": False, "optional": True, "status": HOSTED,
         "enables": "Recording your microphone and the other side's audio, and the live coach during a call.",
         "detail": "Recording happens on a Mac, not on a server. Bring calls in from a recorder (Fireflies, Fathom, "
                   "the webhook) or upload the transcript.",
         "fix": ""},
        {"key": "speech", "title": "Speech-to-text models", "ok": False, "optional": True, "status": HOSTED,
         "enables": "Turns recorded audio into a transcript. Not needed for transcripts that come from a recorder.",
         "detail": "Local transcription needs Apple Silicon. Upload transcripts rather than recordings.",
         "fix": ""},
        {"key": "cli", "title": "Claude CLI", "ok": False, "optional": True, "status": HOSTED,
         "enables": "The “Claude subscription” model option, the calendar and Granola.",
         "detail": "The Claude CLI is not part of the container. Use a provider with an API key (Anthropic, OpenAI, "
                   "xAI or an OpenAI-compatible gateway).",
         "fix": ""},
    ]


def _cloud_google_rows() -> list[dict]:
    """Cloud: Gmail and Calendar are each rep's own connection (on /me/setup); what the org can check
    here is whether Google is set up at all: the client, the allowed domains, the token keys."""
    from .. import googleauth
    from ..execution import tokens
    problems = googleauth.problems()
    keys = tokens.keys_configured()
    ok = not problems and keys
    domains = ", ".join(googleauth.allowed_domains()) or "none"
    detail = (f"Google sign-in is configured for {domains}; each person connects their own Gmail and Calendar "
              "from their profile page (You)." if ok else
              "Not ready: " + "; ".join(problems + ([f"{tokens.KEYS_ENV} is not set"] if not keys else [])) + ".")
    fix = "" if ok else ("Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_ALLOWED_DOMAINS and SALESCOACH_TOKEN_KEYS "
                         "on the host (docs/deploy-cloud.md), then restart.")
    return [
        {"key": "gmail", "title": "Gmail: sending and reading replies", "ok": ok, "optional": False,
         "status": "Ready for each person to connect" if ok else "Not configured",
         "enables": "Each rep sends follow-ups from their own mailbox and the coach reads replies to them. "
                    "Scopes asked for: gmail.compose and gmail.readonly, per person, on their profile page.",
         "detail": detail, "fix": fix},
        {"key": "calendar", "title": "Calendar", "ok": ok, "optional": True,
         "status": "Ready for each person to connect" if ok else "Not configured",
         "enables": "Each rep's meetings, read-only (calendar.readonly), for the Today page and the follow-ups.",
         "detail": detail, "fix": fix},
    ]


def connections(conn, live: dict | None = None) -> list[dict]:
    """Read-only status rows: {key, title, ok, optional, status, enables, detail, fix}."""
    if hosted.is_cloud():
        return _cloud_google_rows() + [r for r in _hosted_connections() if r["key"] not in ("gmail", "calendar")]
    if hosted.is_hosted():
        return _hosted_connections()
    rows = []
    cli = providers.claude_cli_available()

    alias = (config.load("policy").get("email") or {}).get("account", "work")
    try:
        client, token = _gmail_token(alias)
    except Exception:
        client = token = False
    rows.append({
        "key": "gmail", "title": "Gmail: sending and reading replies", "ok": client and token, "optional": False,
        "status": "Ready" if client and token else "Not connected",
        "enables": "Sends the follow-up email when you press Send, and reads replies to the emails it sent. "
                   "Without it, drafts are still written; you copy them into your own mail.",
        "detail": f"Sending account: “{alias}”. " + (
            "A sign-in for it is stored on this machine." if client and token else
            "No stored sign-in was found for it." if client else "The Gmail connection has not been set up on this machine."),
        "fix": "" if client and token else
               f"Ask whoever installed the coach to connect the “{alias}” Gmail account (the gmail-multi tool "
               "stores the sign-in under .gmail-mcp in your home folder). Nothing is sent until you press Send."})

    try:
        discovered = json.loads(get_state(conn, "automation:calendar_tools") or "null")
    except ValueError:
        discovered = None
    seen = bool(cli and isinstance(discovered, dict) and discovered.get("tools"))
    rows.append({
        "key": "calendar", "title": "Calendar", "ok": seen, "optional": False,
        "status": "Ready" if seen else ("Not checked yet" if cli else "Needs the Claude CLI"),
        "enables": "Lists your meetings on the Today page, arms recording for them, and fills real free times "
                   "into follow-up emails. Read-only: it never creates or answers an event.",
        "detail": (f"Your Google Calendar was last reached on {str(discovered.get('discovered_at'))[:10]}." if seen else
                   "The coach has not reached your calendar yet." if cli else
                   "The calendar is read through the Claude CLI, which is not installed."),
        "fix": "" if seen else (
            "Open the Calendar page and press Refresh calendar. If it fails, authorise the Google Calendar "
            "connector in your Claude settings, then try again." if cli else
            "Install the Claude CLI and sign in, then authorise the Google Calendar connector in your Claude settings.")})

    binary = capture_binary()
    built = binary.is_file() and os.access(binary, os.X_OK)
    available = bool((live or {}).get("available"))
    rows.append({
        "key": "capture", "title": "Recording calls on this Mac", "ok": built and available, "optional": True,
        "status": "Ready" if built and available else ("Not built" if not built else "Not running"),
        "enables": "Start call records your microphone and the other side's audio, and the live coach "
                   "nudges you during the call. Without it, bring calls in from a recorder or a file.",
        "detail": ("The recorder is installed." if built else "The recorder has not been built on this machine.") + (
            "" if available or not built else " This server was started without live capture."),
        "fix": ("macOS must allow it once: System Settings, Privacy and Security, then Microphone, and Screen and "
                "System Audio Recording (System Audio Recording Only). Without that it records silence. The coach "
                "cannot check this permission for you." if built else
                "Ask whoever installed the coach to run callcap/build.sh, then allow Microphone and System Audio "
                "Recording in System Settings.")})

    try:
        have, missing = _speech_models()
        checked = True
    except Exception:
        have, missing, checked = [], [], False
    rows.append({
        "key": "speech", "title": "Speech-to-text models", "ok": checked and bool(have) and not missing,
        "optional": True,
        "status": "Ready" if checked and have and not missing else ("Could not check" if not checked else "Missing"),
        "enables": "Turns recorded audio into a transcript on this machine. Not needed for transcripts that "
                   "come from a recorder.",
        "detail": (f"{len(have)} of {len(have) + len(missing)} models are downloaded." if checked else
                   "The speech models could not be checked."),
        "fix": "" if checked and not missing and have else
               "Download each missing model once, in a terminal: " +
               "; ".join(f"salescoach models pull {repo}" for repo in missing) if missing else ""})

    rows.append({
        "key": "cli", "title": "Claude CLI", "ok": cli, "optional": True,
        "status": "Installed" if cli else "Not installed",
        "enables": "Needed for the calendar and for Granola, and for the “Claude subscription” model option. "
                   "Not needed if you use an API key and another recorder.",
        "detail": "Found on this machine." if cli else "Not found on this machine.",
        "fix": "" if cli else "Install the Claude CLI, run it once in a terminal and sign in."})
    return rows


# ---------------------------------------------------------------------------------------- rail

def steps(conn, live: dict | None = None, rows: list | None = None) -> list[dict]:
    """The progress rail: {key, title, href, status, note}; status is done | default | attention | todo.
    `default` = works as shipped, nothing chosen yet. Only step 1 can block the app."""
    out = []

    def add(key, status, note):
        out.append({"key": key, "title": TITLES[key], "href": href(key), "status": status, "note": note})

    configured = seller.is_configured()
    add("you", "done" if configured else "attention", seller.company() if configured else "Needed to start")

    active = methodology.active()
    chosen = bool(config.load_user("methodology").get("active"))
    add("method", "done" if chosen else "default", active.name if chosen else f"Default: {active.name}")

    p = provider_state(conn)
    if p["usable"] and p["tested_ok"]:
        add("model", "done", p["short"])
    elif p["usable"] and not p["chosen"]:
        add("model", "default", f"Default: {p['short']}, not tested")
    else:
        add("model", "attention", "Not tested yet" if p["usable"] else "Needs a provider")

    s = sources_state(conn)
    if identity.cloud():
        # Cloud: the step is the org's allow-list of recorders each rep may connect (setup_sources_cloud.html).
        from ..sources import connections
        allowed = [r["label"] for r in connections.catalog() if r["allowed"]]
        if connections.allowed_is_default():
            add("sources", "default", "All recorders allowed")
        else:
            add("sources", "done", ("Allowed: " + ", ".join(allowed)) if allowed else "No recorders allowed")
    elif s["chosen"] or s["extra"]:
        add("sources", "done", ", ".join(d["label"] for d in s["extra"]) or "Upload and folder")
    else:
        add("sources", "default", "Default: upload and folder")

    rows = connections(conn, live) if rows is None else rows
    ready = sum(1 for r in rows if r["ok"])
    needed = [r for r in rows if not r["optional"]]
    add("connections", "done" if all(r["ok"] for r in needed) else "attention", f"{ready} of {len(rows)} ready")

    finished = bool(get_state(conn, FINISHED_KEY))
    add("review", "done" if finished and configured else "todo", "Finished" if finished and configured else "")
    return out


def missing(conn, live: dict | None = None, rows: list | None = None) -> list[dict]:
    """What the review step lists as still to do: {step, text, blocking}."""
    found = []
    if not seller.is_configured():
        found.append({"step": "you", "blocking": True,
                      "text": "Your profile: your name, one email address, your company and what you sell."})
    p = provider_state(conn)
    if not p["usable"]:
        found.append({"step": "model", "blocking": False, "text": f"The model provider cannot be used yet. {p['why']}"})
    elif not p["tested_ok"]:
        found.append({"step": "model", "blocking": False,
                      "text": f"{p['label']} has not passed a connection test yet."})
    for row in (connections(conn, live) if rows is None else rows):
        if not row["ok"] and not row["optional"]:
            found.append({"step": "connections", "blocking": False, "text": f"{row['title']}: {row['status'].lower()}."})
    return found


# --------------------------------------------------------------------------------------- Today

def card_signature(p: dict) -> str:
    """What a dismissal applies to: this provider in this condition. A provider that stops being usable,
    or a change of provider, brings the card back."""
    return f"{p['key']}:{'untested' if p['usable'] else 'unusable'}"


def today_card(conn) -> dict | None:
    """The small "Finish setting up" card: shown while no provider can be used or none has passed a
    test, until it is dismissed for that provider."""
    p = provider_state(conn)
    if p["usable"] and p["tested_ok"]:
        return None
    if get_user_state(conn, DISMISSED_KEY) == card_signature(p):
        return None
    return {"provider": p["label"], "signature": card_signature(p), "usable": p["usable"], "why": p["why"],
            "text": (f"{p['label']} has not passed a connection test yet. One click checks that the coach can "
                     "reach its model." if p["usable"] else
                     f"The coach cannot analyse calls yet. {p['why']}")}
