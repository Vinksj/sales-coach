"""Transcript sources: every way a call that was not captured live gets in.

  base.py      NormalizedTranscript + import_normalized: the one import path
  parsers.py   export formats -> turns (format sniffing)
  adapters/    upload, folder, webhook, fireflies, fathom, granola
  paste.py, granola.py, audio_file.py   the original three, the first two now on base.py

Settings and polling (what the setup UI and the scheduler call):
  catalog(conn=None)                         -> [descriptor]   every source, with its state
  settings()                                 -> {kind: {enabled, poll_minutes, options}}
  save(kind, enabled, poll_minutes=None, options=None) -> descriptor
  new_webhook_secret()                       -> the secret, shown once
  poll(conn, kinds=None, force=False, adapters=None) -> {kind: result}

The user's choices live in the user overlay `sources.yaml`:
  sources:   [{kind, enabled, poll_minutes, options}]
  me_labels: [labels the seller said are theirs; base.remember_me_label]
Keys are never in that file: config.set_secret(descriptor["api_key_env"], value).
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import config

log = logging.getLogger("salescoach.sources")
MIN_POLL_MINUTES, MAX_POLL_MINUTES = 1, 24 * 60
FIRST_LOOKBACK_DAYS = 2          # a newly enabled recorder brings in the last two days, not its whole archive
OVERLAP = timedelta(hours=6)     # recorders finish transcripts late; look back past the last poll
MAX_PER_POLL = 25


# ---------------------------------------------------------------------------------------- settings

def _saved() -> dict:
    out = {}
    for entry in config.load("sources").get("sources") or []:
        if isinstance(entry, dict) and entry.get("kind"):
            out[str(entry["kind"])] = entry
    return out


def settings() -> dict:
    """{kind: {enabled, poll_minutes, options}} for every adapter: the saved choice over the adapter's default."""
    from . import adapters
    saved, out = _saved(), {}
    for kind, cls in adapters.classes().items():
        entry = saved.get(kind) or {}
        minutes = entry.get("poll_minutes", cls.default_poll_minutes)
        try:
            minutes = None if cls.mode != "poll" else max(MIN_POLL_MINUTES, min(MAX_POLL_MINUTES, int(minutes)))
        except (TypeError, ValueError):
            minutes = cls.default_poll_minutes
        out[kind] = {"enabled": bool(entry.get("enabled", cls.default_enabled)) or kind == "upload",
                     "poll_minutes": minutes,
                     "options": dict(entry.get("options") or {}) if isinstance(entry.get("options"), dict) else {}}
    return out


def save(kind: str, enabled: bool, poll_minutes: Optional[int] = None, options: Optional[dict] = None) -> dict:
    """Save one source's choice to the user's sources.yaml and return its fresh descriptor.
    Raises ValueError for an unknown kind, a poll interval out of range, or options that are not
    a flat mapping of scalars. API keys do not go through here (config.set_secret)."""
    from . import adapters
    classes = adapters.classes()
    if kind not in classes:
        raise ValueError(f"unknown source {kind!r}; one of {', '.join(classes)}")
    cls = classes[kind]
    entry = {"kind": kind, "enabled": bool(enabled) or kind == "upload"}
    if cls.mode == "poll":
        minutes = cls.default_poll_minutes if poll_minutes in (None, "") else poll_minutes
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise ValueError("poll_minutes must be a whole number of minutes") from None
        if not MIN_POLL_MINUTES <= minutes <= MAX_POLL_MINUTES:
            raise ValueError(f"poll_minutes must be between {MIN_POLL_MINUTES} and {MAX_POLL_MINUTES}")
        entry["poll_minutes"] = minutes
    options = dict(options or {})
    for key, value in options.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)) or len(str(value)) > 500:
            raise ValueError("options must be a flat mapping of short text, numbers or yes/no values")
    if kind == "folder" and options.get("path"):
        from .adapters import folder
        problem = folder.refusal(options["path"])
        if problem:
            raise ValueError(problem)
    entry["options"] = options
    data = config.load_user("sources")
    others = [e for e in data.get("sources") or [] if isinstance(e, dict) and e.get("kind") != kind]
    data["sources"] = [*others, entry]
    config.save_user("sources", data)
    return next(d for d in catalog() if d["kind"] == kind)


def new_webhook_secret() -> str:
    """Create (or replace) the webhook's shared secret and return it ONCE, for the user to paste into
    their automation. It is never readable again through the app."""
    import secrets as _secrets
    from .adapters import webhook
    value = _secrets.token_urlsafe(32)
    config.set_secret(webhook.SECRET_NAME, value)
    return value


def catalog(conn=None) -> list:
    """Descriptors for the setup UI, adapters first, then the recorders that only export.

    {kind, label, how, mode: push|poll|export, needs_key, api_key_env, verified, configured, enabled,
     poll_minutes, options, last_run, last_error}
    `verified: False` must be shown as "untested against the live API". last_run / last_error
    are filled when a connection is given. No descriptor ever contains a secret's value."""
    from ..store.stores import get_state
    from . import adapters
    chosen, out = settings(), []
    for kind, cls in adapters.classes().items():
        adapter = cls(chosen[kind]["options"])
        try:
            configured = bool(adapter.configured())
        except Exception:                                  # a broken adapter is "not configured", not a crash
            log.exception("source %s: configured() failed", kind)
            configured = False
        last_error = get_state(conn, f"sources:{kind}:last_error") if conn is not None else None
        out.append({"kind": kind, "label": cls.label, "how": cls.how, "mode": cls.mode,
                    "needs_key": cls.needs_key, "api_key_env": cls.api_key_env, "verified": cls.verified,
                    "configured": configured, "enabled": chosen[kind]["enabled"],
                    "poll_minutes": chosen[kind]["poll_minutes"], "options": chosen[kind]["options"],
                    "last_run": get_state(conn, f"sources:{kind}:last_run") if conn is not None else None,
                    "last_error": json.loads(last_error) if last_error else None})
    for kind, label, how in adapters.EXPORT_ONLY:
        out.append({"kind": kind, "label": label, "how": how, "mode": "export", "needs_key": False,
                    "api_key_env": None, "verified": True, "configured": True, "enabled": True,
                    "poll_minutes": None, "options": {}, "last_run": None, "last_error": None})
    return out


# ----------------------------------------------------------------------------------------- polling

def _due(conn, kind: str, minutes: int, moment: datetime) -> bool:
    from ..store.stores import get_state
    last = get_state(conn, f"sources:{kind}:last_run")
    if not last:
        return True
    try:
        return moment - datetime.fromisoformat(last) >= timedelta(minutes=minutes) - timedelta(seconds=5)
    except ValueError:
        return True


def _since(conn, kind: str, moment: datetime) -> datetime:
    from ..store.stores import get_state
    last = get_state(conn, f"sources:{kind}:last_ok")
    try:
        return datetime.fromisoformat(last) - OVERLAP if last else moment - timedelta(days=FIRST_LOOKBACK_DAYS)
    except ValueError:
        return moment - timedelta(days=FIRST_LOOKBACK_DAYS)


def _internal_only(emails) -> bool:
    """Everyone else on the invite is a colleague. With nobody else known, it is not provably internal."""
    from .. import seller
    own = {e.lower() for e in seller.emails()}
    others = [e.lower() for e in emails if e and "@" in e and e.lower() not in own]
    return bool(others) and all(e.rsplit("@", 1)[-1] in seller.internal_domains() for e in others)


def _after_failure(adapter, ext_id, error, exc) -> None:
    """Adapters written before the hook carried the exception take two arguments."""
    import inspect
    try:
        takes_exc = "exc" in inspect.signature(adapter.after_failure).parameters
    except (TypeError, ValueError):
        takes_exc = False
    if takes_exc:
        adapter.after_failure(ext_id, error, exc=exc)
    else:
        adapter.after_failure(ext_id, error)


def poll_one(conn, adapter, since: Optional[datetime] = None, only_deals: bool = False) -> dict:
    """List one adapter's recent meetings and import the new ones. One bad meeting is recorded and
    skipped; a failure to LIST raises (the caller records it against the source)."""
    from ..store.stores import get_state, set_state
    from . import base
    result = {"listed": 0, "imported": [], "needs_speaker": [], "skipped": 0, "errors": []}
    if hasattr(adapter, "use_state"):
        adapter.use_state(lambda key: get_state(conn, f"sources:{adapter.kind}:{key}"),
                          lambda key, value: set_state(conn, f"sources:{adapter.kind}:{key}", value))
    refs = adapter.list_recent(since)
    conn.commit()                                       # what list_recent set aside (a recovered leftover)
    result["listed"] = len(refs)
    for ref in refs[:MAX_PER_POLL]:
        try:
            if ref.source_ref and base.existing_call(conn, ref.source_ref):
                result["skipped"] += 1
                continue
            if ref.emails and _internal_only(ref.emails):
                result["skipped"] += 1                     # a team meeting is not a sales call
                continue
            nt = adapter.fetch(ref.ext_id)
            deal_id = base.guess_deal(conn, nt)
            if only_deals and not deal_id:
                result["skipped"] += 1
                continue
            # link="account": nobody watched this import, so only addresses at the deal's own account
            # domains are put on the deal; the rest stay participants of this call.
            outcome = base.import_normalized(conn, nt, deal_id=deal_id, history=False, add_me=True, link="account")
            adapter.after_import(ref.ext_id, outcome)
            if outcome.created:
                result["needs_speaker" if outcome.needs_speaker else "imported"].append(outcome.call_id)
            else:
                result["skipped"] += 1
        except Exception as exc:
            conn.rollback()
            error = f"{type(exc).__name__}: {exc}"[:500]
            log.warning("source %s: %s failed: %s", adapter.kind, ref.ext_id, error)
            result["errors"].append({"id": ref.ext_id, "error": error})
            try:
                _after_failure(adapter, ref.ext_id, error, exc)
                conn.commit()                           # the next failure's rollback must not undo this one's note
            except Exception:
                log.exception("source %s: after_failure(%s) failed", adapter.kind, ref.ext_id)
    return result


def poll(conn, kinds=None, force: bool = False, adapters: Optional[dict] = None, moment: Optional[datetime] = None
         ) -> dict:
    """Poll every enabled, configured, due poll-adapter. One adapter's failure never stops the others:
    it is recorded in state `sources:<kind>:last_error` and the loop moves on. Commits per source.

    adapters: {kind: adapter instance}, for tests and for callers that hold a configured instance."""
    from ..store.stores import set_state
    from . import adapters as adapters_pkg
    moment = moment or datetime.now(timezone.utc)
    chosen, results = settings(), {}
    for kind, cls in adapters_pkg.classes().items():
        if cls.mode != "poll" or (kinds and kind not in kinds):
            continue
        choice = chosen[kind]
        if not choice["enabled"]:
            continue
        if not force and not _due(conn, kind, choice["poll_minutes"] or cls.default_poll_minutes, moment):
            continue
        stamp = moment.isoformat(timespec="seconds")
        try:
            adapter = (adapters or {}).get(kind) or cls(choice["options"])
            if not adapter.configured():
                results[kind] = {"skipped": "not configured"}
                continue
            result = poll_one(conn, adapter, since=_since(conn, kind, moment),
                              only_deals=bool(choice["options"].get("only_deals")))
            set_state(conn, f"sources:{kind}:last_run", stamp)
            set_state(conn, f"sources:{kind}:last_result", json.dumps(result, default=str)[:4000])
            # The LIST worked, so the window moves on; a meeting that failed is retried while it is still
            # inside OVERLAP, and is on record here either way.
            set_state(conn, f"sources:{kind}:last_ok", stamp)
            set_state(conn, f"sources:{kind}:last_error", json.dumps(
                {"at": stamp, "error": f"{len(result['errors'])} meeting(s) failed: "
                                       f"{result['errors'][0]['error']}"[:1000]}) if result["errors"] else "")
            conn.commit()
            results[kind] = result
        except Exception as exc:
            log.exception("source %s: poll failed", kind)
            error = f"{type(exc).__name__}: {exc}"[:1000]
            try:
                conn.rollback()
                set_state(conn, f"sources:{kind}:last_run", stamp)
                set_state(conn, f"sources:{kind}:last_error", json.dumps({"at": stamp, "error": error}))
                conn.commit()
            except Exception:
                log.exception("source %s: could not record the failure", kind)
            results[kind] = {"error": error}
    return results
