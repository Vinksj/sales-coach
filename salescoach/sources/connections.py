"""Per-rep recorder connections (Phase 4): each rep's own recorder account, and the calls it delivers.

THE MODEL. A call belongs to the user whose recorder connection delivered it. Each rep connects THEIR
OWN account (Fathom, Fireflies, tl;dv, Granola) on /me/setup; an admin only decides which kinds the org
allows (org setting sources.allowed_kinds, Setup > "Where calls come from") and never connects an
account for anyone. Two reps on the same meeting each get their own call from their own account:
source_ref = "<kind>:<owner>:<id at the recorder>", so the copies never collide, and each is analysed from
its owner's side. There is no organiser matching, no unassigned queue, no duplicate merging and no
shared-call access; nothing in this module can put a call anywhere but on the connection's owner.

  source_connections   one row per (owner, kind), OWNED (store/tenancy.py). The API key is AES-256-GCM
                       ciphertext under the SALESCOACH_TOKEN_KEYS ring (execution/tokens.py), bound to
                       (owner, "recorder:<kind>", column) as associated data, so a ciphertext moved to
                       another row does not decrypt. Keys are write-only: no function here returns one
                       to a caller that renders, and public() strips every secret column.
  status               active        polled when due
                       error         the recorder refused the key (401/403), or the stored key can no
                                     longer be decrypted: polling stops until the rep reconnects
                       disconnected  the rep disconnected: key and webhook secrets deleted; the row stays
                                     so "My meetings" still shows what it brought in
  state (json)         account {email, name}   who the key belongs to, when the API says
                       recent [...]            the recorder's recent listing and what became of each
                                               meeting (new / pending / imported / needs_speaker /
                                               internal / failed), what /me/meetings shows
                       cursor                  where a capped listing stopped
                       rate_limited_until      after a 429, remembered so no poll hits the limit again

POLLING (the per-user `recorders` duty, plugins/sources.py). Inside each active user's service session,
poll_user() polls that user's own due connections: list since the last good poll minus OVERLAP (a
first poll looks back FIRST_LOOKBACK_DAYS), import what is new with
import_normalized(owner=<the connection's user>, history=False, link="account"), deal from the
participants (onboard.deal_for_emails), speakers from that user's aliases and remembered labels. Errors
back off exponentially (next_poll_at), a 429 waits as long as the recorder asked, a 401/403 stops the
connection with a "reconnect" message. One connection's failure never stops another's, and the duty
runs every user in turn whatever any one of them did.

WEBHOOK. POST /import/webhook/{connection_id} (sources/web.py): the connection's owner is looked up
with nobody bound (app_source_connection_owner on Postgres), the store is bound to that owner, the push
is verified (the recorder's Standard Webhooks signature where it signs, else the per-connection token
header; recorders/signatures.py), and the meeting it names is fetched with the owner's own key and
imported as the owner. The payload can never name an owner; a disconnected connection is 404.

The org-level sources (sources/__init__.py: folder, the org webhook, org API keys) are a LOCAL install's
and are unchanged there; in cloud mode they do not run.
"""
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import config, identity
from ..execution import tokens
from ..store.stores import now
from . import base, recorders
from .adapters import SourceAuthError, SourceError, SourceNotFound, SourceRateLimited
from .recorders import Account, signatures

log = logging.getLogger("salescoach.sources.connections")

TRANSPORT = None                  # tests: an httpx.MockTransport every recorder API call goes through
STATUSES = ("active", "error", "disconnected")
FIRST_LOOKBACK_DAYS = 2           # a new connection brings in the last two days, not the account's archive
OVERLAP = timedelta(hours=6)      # recorders finish transcripts late; look back past the last good poll
RETRY_DAYS = 3                    # a listed meeting not imported yet is retried while it is this recent
MAX_ATTEMPTS = 5                  # ... and at most this many times when it keeps failing
MAX_RECENT = 100
MAX_PER_POLL = 25
MAX_BACKOFF = timedelta(hours=6)
RATE_LIMIT_DEFAULT = timedelta(hours=1)
ID_RE = re.compile(r"rc-[0-9a-f]{32}")
SECRET_COLUMNS = ("secret_enc", "key_id", "webhook_token_hash", "webhook_secret_enc")


class ConnectionError_(ValueError):
    """A connection request the rep can fix (unknown or disallowed kind, no key typed)."""


# ---- the org's policy -----------------------------------------------------------------------------

def kinds() -> dict:
    return recorders.classes()


def allowed_kinds() -> list:
    """The recorder kinds this org lets its reps connect: sources.allowed_kinds, in catalog order. Unset
    (an admin never chose): every supported kind."""
    raw = config.load("sources").get("allowed_kinds")
    every = list(kinds())
    if raw is None:
        return every
    chosen = {str(k) for k in raw} if isinstance(raw, (list, tuple)) else set()
    return [k for k in every if k in chosen]


def allowed_is_default() -> bool:
    return config.load("sources").get("allowed_kinds") is None


def set_allowed_kinds(chosen) -> list:
    """An admin's choice (Setup > Where calls come from, cloud mode). Unknown kinds are refused."""
    chosen = [str(k) for k in chosen or ()]
    unknown = [k for k in chosen if k not in kinds()]
    if unknown:
        raise ConnectionError_(f"unknown recorder {unknown[0]!r}; one of {', '.join(kinds())}")
    data = config.load_user("sources")
    data["allowed_kinds"] = [k for k in kinds() if k in chosen]
    config.save_user("sources", data)
    return allowed_kinds()


def catalog() -> list:
    """What the admin allow-list page and the rep's cards describe, one entry per supported recorder.
    Nothing about any rep's connection."""
    allowed = set(allowed_kinds())
    return [{"kind": k, "label": c.label, "where_key": c.where_key, "plan": c.plan, "rate_note": c.rate_note,
             "verified": c.verified, "poll_minutes": c.default_poll_minutes, "webhook_scheme": c.webhook_scheme,
             "allowed": k in allowed} for k, c in kinds().items()]


# ---- rows ---------------------------------------------------------------------------------------

def _owner(conn) -> str:
    return identity.actor_of(conn).user_id


def _row(r) -> Optional[dict]:
    if r is None:
        return None
    out = dict(r)
    try:
        out["state"] = json.loads(out.get("state") or "{}")
        if not isinstance(out["state"], dict):
            out["state"] = {}
    except ValueError:
        out["state"] = {}
    return out


def public(row: Optional[dict]) -> Optional[dict]:
    """A row as a page may show it: every secret column removed, flags for what is set."""
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k not in SECRET_COLUMNS}
    out["has_key"] = bool(row.get("secret_enc"))
    out["has_webhook_token"] = bool(row.get("webhook_token_hash"))
    out["has_signing_secret"] = bool(row.get("webhook_secret_enc"))
    return out


def list_mine(conn) -> list:
    """The acting user's own connections (explicitly theirs: a manager's session reads its team's rows under
    the policies, and nothing here ever acts on anyone else's)."""
    return [_row(r) for r in conn.execute("SELECT * FROM source_connections WHERE owner_id=? ORDER BY kind",
                                          (_owner(conn),)).fetchall()]


def get(conn, connection_id: str) -> Optional[dict]:
    if not ID_RE.fullmatch(str(connection_id or "")):
        return None
    return _row(conn.execute("SELECT * FROM source_connections WHERE id=? AND owner_id=?",
                             (connection_id, _owner(conn))).fetchone())


def get_kind(conn, kind: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM source_connections WHERE owner_id=? AND kind=?",
                             (_owner(conn), kind)).fetchone())


# ---- secrets --------------------------------------------------------------------------------------

def _aad(owner: str, kind: str, column: str) -> bytes:
    return tokens._aad(owner, f"recorder:{kind}", column)


def _open(row: dict, column: str, name: str) -> Optional[str]:
    if not row.get(column):
        return None
    return tokens.decrypt(row[column], row["key_id"], _aad(row["owner_id"], row["kind"], name))


def api_key_of(row: dict) -> str:
    """The plaintext key, for the adapter's request header only. Raises tokens.TokenError."""
    key = _open(row, "secret_enc", "api_key")
    if not key:
        raise tokens.NoToken(f"no {row['kind']} key is stored for this connection")
    return key


def _signing_secret(row: dict) -> Optional[str]:
    return _open(row, "webhook_secret_enc", "signing_secret")


def _seal(owner: str, kind: str, api_key: Optional[str], signing: Optional[str]) -> tuple:
    """(secret_enc, webhook_secret_enc, key_id): both under the newest key, so one key_id covers the row."""
    kid = tokens.key_ring()[0][0]
    key_ct = tokens.encrypt(api_key, _aad(owner, kind, "api_key"))[0] if api_key else None
    sig_ct = tokens.encrypt(signing, _aad(owner, kind, "signing_secret"))[0] if signing else None
    return key_ct, sig_ct, kid if (key_ct or sig_ct) else None


def _clean_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        raise ConnectionError_("Paste your API key first.")
    if len(key) > 500 or re.search(r"[\s\"'<>]", key):
        raise ConnectionError_("That does not look like an API key: it cannot contain spaces, quotes or line breaks.")
    return key


def _require_kind(kind: str):
    if kind not in kinds():
        raise ConnectionError_("That is not a recorder the coach can connect.")
    if kind not in allowed_kinds():
        raise ConnectionError_(f"{kinds()[kind].label} is not allowed on this install; ask your admin.")
    return kinds()[kind]


def _my_emails() -> list:
    """The acting user's own addresses (their profile): how an adapter tells the rep from a colleague."""
    from .. import seller
    try:
        return list(seller.emails())
    except Exception:                                  # no profile yet: the adapter does not guess
        return []


def adapter_for(row: dict, api_key: Optional[str] = None):
    account = (row.get("state") or {}).get("account") or {}
    return recorders.build(row["kind"], api_key or api_key_of(row), row["owner_id"], transport=TRANSPORT,
                           account=Account(email=account.get("email"), name=account.get("name")),
                           me_emails=_my_emails())


def test_key(conn, kind: str, api_key: Optional[str] = None) -> Account:
    """The Connect card's Test: one cheap authenticated call with the typed key, or with the stored one when
    nothing was typed. Raises ConnectionError_, SourceAuthError, SourceError, tokens.TokenError."""
    cls = _require_kind(kind)
    if api_key and api_key.strip():
        key = _clean_key(api_key)
    else:
        row = get_kind(conn, kind)
        if row is None or not row.get("secret_enc"):
            raise ConnectionError_(f"Paste your {cls.label} API key first.")
        key = api_key_of(row)
    return recorders.build(kind, key, _owner(conn), transport=TRANSPORT, me_emails=_my_emails()).test()


def save_key(conn, kind: str, api_key: str, account: Optional[Account] = None) -> dict:
    """Connect (or reconnect) the acting user's own `kind` account with `api_key`. The row is theirs, the
    key is encrypted, the connection is active and due at once. Commits. Returns public(row)."""
    cls = _require_kind(kind)
    key = _clean_key(api_key)
    owner, stamp = _owner(conn), now()
    existing = get_kind(conn, kind)
    signing = None
    if existing and existing.get("webhook_secret_enc"):
        try:
            signing = _signing_secret(existing)
        except tokens.TokenError:
            signing = None                             # unreadable: the rep pastes it again
    key_ct, sig_ct, kid = _seal(owner, kind, key, signing)
    state = dict((existing or {}).get("state") or {})
    state.pop("rate_limited_until", None)
    if account and (account.email or account.name):
        state["account"] = {"email": account.email, "name": account.name}
    elif existing and existing.get("status") == "disconnected":
        state.pop("account", None)                     # a new key may be another account
    if existing:
        conn.execute(
            "UPDATE source_connections SET secret_enc=?, key_id=?, webhook_secret_enc=?, status='active', "
            "account_email=?, state=?, failures=0, last_error=NULL, next_poll_at=NULL, label=?, updated_at=? "
            "WHERE id=? AND owner_id=?",
            (key_ct, kid, sig_ct, (state.get("account") or {}).get("email"), json.dumps(state), cls.label, stamp,
             existing["id"], owner))
        connection_id = existing["id"]
    else:
        connection_id = "rc-" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO source_connections(id,owner_id,kind,label,secret_enc,key_id,status,account_email,state,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,'active',?,?,?,?)",
            (connection_id, owner, kind, cls.label, key_ct, kid, (state.get("account") or {}).get("email"),
             json.dumps(state), stamp, stamp))
    conn.commit()
    return public(get(conn, connection_id))


def disconnect(conn, connection_id: str) -> bool:
    """The rep's Disconnect: the key and both webhook secrets are deleted, polling and the webhook stop.
    The row stays (disconnected) so their meetings page still shows what came in. Commits."""
    row = get(conn, connection_id)
    if row is None:
        return False
    conn.execute("UPDATE source_connections SET status='disconnected', secret_enc=NULL, key_id=NULL, "
                 "webhook_token_hash=NULL, webhook_secret_enc=NULL, next_poll_at=NULL, updated_at=? "
                 "WHERE id=? AND owner_id=?", (now(), row["id"], row["owner_id"]))
    conn.commit()
    return True


def new_webhook_token(conn, connection_id: str) -> Optional[str]:
    """A fresh per-connection webhook token, returned ONCE (only its sha256 is kept). None: not the rep's,
    or not connected."""
    row = get(conn, connection_id)
    if row is None or row["status"] == "disconnected":
        return None
    token = "whk_" + secrets.token_urlsafe(32)
    conn.execute("UPDATE source_connections SET webhook_token_hash=?, updated_at=? WHERE id=? AND owner_id=?",
                 (signatures.token_hash(token), now(), row["id"], row["owner_id"]))
    conn.commit()
    return token


def set_signing_secret(conn, connection_id: str, secret: str) -> bool:
    """The signing secret the recorder showed when the rep created the webhook there (Fathom, Granola):
    kept encrypted (an HMAC is checked with the secret itself, not a hash of it). Commits."""
    row = get(conn, connection_id)
    if row is None or row["status"] == "disconnected":
        return False
    secret = (secret or "").strip()
    if not secret or len(secret) > 300 or re.search(r"\s", secret):
        raise ConnectionError_("Paste the signing secret exactly as the recorder shows it.")
    api_key = api_key_of(row) if row.get("secret_enc") else None
    key_ct, sig_ct, kid = _seal(row["owner_id"], row["kind"], api_key, secret)
    conn.execute("UPDATE source_connections SET secret_enc=?, webhook_secret_enc=?, key_id=?, updated_at=? "
                 "WHERE id=? AND owner_id=?", (key_ct, sig_ct, kid, now(), row["id"], row["owner_id"]))
    conn.commit()
    return True


def rotate(conn) -> dict:
    """Re-encrypt every connection the connection can see under the newest key (`salescoach tokens rotate`
    calls it after the OAuth grants). {rotated, skipped, unreadable}."""
    newest = tokens.key_ring()[0][0]
    rotated = skipped = 0
    unreadable = []
    for r in conn.execute("SELECT * FROM source_connections ORDER BY owner_id, kind").fetchall():
        row = _row(r)
        if not (row.get("secret_enc") or row.get("webhook_secret_enc")) or row["key_id"] == newest:
            skipped += 1
            continue
        try:
            key, signing = _open(row, "secret_enc", "api_key"), _signing_secret(row)
        except tokens.TokenError:
            unreadable.append(f"{row['owner_id']}/{row['kind']} (key {row['key_id']})")
            continue
        key_ct, sig_ct, kid = _seal(row["owner_id"], row["kind"], key, signing)
        conn.execute("UPDATE source_connections SET secret_enc=?, webhook_secret_enc=?, key_id=?, updated_at=? "
                     "WHERE id=? AND owner_id=?", (key_ct, sig_ct, kid, now(), row["id"], row["owner_id"]))
        rotated += 1
    conn.commit()
    return {"rotated": rotated, "skipped": skipped, "unreadable": unreadable, "key": newest}


# ---- polling --------------------------------------------------------------------------------------

def _ts(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def interval(kind: str) -> timedelta:
    return timedelta(minutes=kinds()[kind].default_poll_minutes)


def backoff(kind: str, failures: int) -> timedelta:
    """The wait after the n-th failure in a row: the poll interval doubled per failure, at most MAX_BACKOFF."""
    return min(MAX_BACKOFF, interval(kind) * (2 ** max(0, min(failures, 10))))


def due(row: dict, moment: datetime) -> bool:
    if row["status"] != "active" or row["kind"] not in allowed_kinds():
        return False
    limited = _ts((row.get("state") or {}).get("rate_limited_until"))
    if limited and moment < limited:
        return False
    nxt = _ts(row.get("next_poll_at"))
    return nxt is None or moment >= nxt - timedelta(seconds=5)


def _record(conn, row: dict, **fields) -> None:
    fields["updated_at"] = now()
    if "state" in fields:
        fields["state"] = json.dumps(fields["state"], default=str)
    names = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE source_connections SET {names} WHERE id=? AND owner_id=?",
                 (*fields.values(), row["id"], row["owner_id"]))


def _fail(conn, row: dict, moment: datetime, error: str, status: str = "active",
          wait: Optional[timedelta] = None, state: Optional[dict] = None) -> dict:
    """Record a failed poll on the row (never lose it to the rollback of the work that failed). Commits."""
    try:
        conn.rollback()
    except Exception:
        pass
    failures = int(row.get("failures") or 0) + 1
    wait = wait or backoff(row["kind"], failures)
    extra = {"state": state} if state is not None else {}
    _record(conn, row, status=status, failures=failures, last_error=error[:1000], last_poll_at=_iso(moment),
            next_poll_at=None if status != "active" else _iso(moment + wait), **extra)
    conn.commit()
    return {"error": error, "status": status}


def reconnect_message(label: str, exc: BaseException) -> str:
    found = re.search(r"HTTP \d{3}", str(exc))
    detail = f" ({found.group(0)})" if found else ""
    return f"{label} refused this key{detail}. Reconnect: paste a new key under Your call recorder."


def _entry(recent: dict, ref) -> dict:
    entry = recent.setdefault(ref.ext_id, {"ext_id": ref.ext_id, "status": "new", "attempts": 0})
    entry.update({"source_ref": ref.source_ref, "title": (ref.title or "")[:300], "started_at": ref.started_at,
                  "emails": list(ref.emails or [])[:30]})
    return entry


def _recent_list(recent: dict) -> list:
    rows = sorted(recent.values(), key=lambda e: e.get("started_at") or "", reverse=True)
    return rows[:MAX_RECENT]


def _candidates(recent: dict, listed_ids: list, moment: datetime) -> list:
    """What to try this poll: the new listing first, then older entries still worth a retry."""
    horizon = moment - timedelta(days=RETRY_DAYS)
    out = [recent[i] for i in listed_ids if recent[i]["status"] in ("new", "pending", "failed")]
    for entry in recent.values():
        if entry["ext_id"] in listed_ids or entry["status"] not in ("new", "pending", "failed"):
            continue
        started = _ts(entry.get("started_at"))
        if started and started >= horizon:
            out.append(entry)
    return [e for e in out if int(e.get("attempts") or 0) < MAX_ATTEMPTS]


def poll_connection(conn, row: dict, moment: Optional[datetime] = None) -> dict:
    """Poll one of the ACTING user's connections: list, import what is new as its owner, record the outcome
    in the row. Never raises for the recorder's sake; commits."""
    from . import _internal_only
    moment = moment or datetime.now(timezone.utc)
    if row["owner_id"] != _owner(conn):
        raise PermissionError("a connection is polled only in its owner's session")
    kind, owner = row["kind"], row["owner_id"]
    label = kinds()[kind].label
    state = dict(row.get("state") or {})
    state.pop("rate_limited_until", None)
    result = {"listed": 0, "imported": [], "needs_speaker": [], "skipped": 0, "pending": 0, "errors": []}
    try:
        adapter = adapter_for(row)
    except tokens.NoKeys as exc:
        return _fail(conn, row, moment, f"the install cannot read stored keys: {exc}")
    except tokens.TokenError:
        return _fail(conn, row, moment, "the stored key can no longer be read. Reconnect: paste your key again "
                                        "under Your call recorder.", status="error")
    last_ok = _ts(row.get("last_ok_at"))
    since = (last_ok - OVERLAP) if last_ok else moment - timedelta(days=FIRST_LOOKBACK_DAYS)
    try:
        refs = adapter.list_recent(since, state.get("cursor"))
    except SourceAuthError as exc:
        return _fail(conn, row, moment, reconnect_message(label, exc), status="error")
    except SourceRateLimited as exc:
        wait = timedelta(seconds=exc.retry_after) if exc.retry_after else RATE_LIMIT_DEFAULT
        wait = max(wait, backoff(kind, int(row.get("failures") or 0) + 1))
        state["rate_limited_until"] = _iso(moment + wait)
        return _fail(conn, row, moment, f"{label} is rate limiting this account; waiting until "
                                        f"{state['rate_limited_until']}", wait=wait, state=state)
    except Exception as exc:                          # SourceError, a bug in one adapter: this connection only
        log.warning("recorder %s of %s: listing failed: %s", kind, owner, type(exc).__name__)
        return _fail(conn, row, moment, f"{type(exc).__name__}: {exc}"[:500])
    result["listed"] = len(refs)
    recent = {e["ext_id"]: e for e in state.get("recent") or [] if isinstance(e, dict) and e.get("ext_id")}
    listed_ids = []
    for ref in refs:
        _entry(recent, ref)
        if ref.ext_id not in listed_ids:
            listed_ids.append(ref.ext_id)
    stop, wait = None, None
    for entry in _candidates(recent, listed_ids, moment)[:MAX_PER_POLL]:
        ext_id = entry["ext_id"]
        source_ref = entry.get("source_ref") or adapter.source_ref(ext_id)
        try:
            found = base.existing_call(conn, source_ref)
            if found:
                entry.update(status="imported", call_id=found)
                result["skipped"] += 1
                continue
            if entry.get("emails") and _internal_only(entry["emails"]):
                entry["status"] = "internal"                  # a team meeting is not a sales call
                result["skipped"] += 1
                continue
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            nt = adapter.fetch(ext_id)
            outcome = base.import_normalized(conn, nt, deal_id=base.guess_deal(conn, nt), history=False,
                                             link="account", owner=owner)
            entry.update(status="needs_speaker" if outcome.needs_speaker else "imported", call_id=outcome.call_id,
                         error=None)
            if outcome.created:
                result["needs_speaker" if outcome.needs_speaker else "imported"].append(outcome.call_id)
            else:
                result["skipped"] += 1
        except SourceNotFound:
            conn.rollback()
            entry["status"] = "pending"                       # listed before its transcript is ready
            result["pending"] += 1
        except SourceAuthError as exc:
            conn.rollback()
            stop = ("error", reconnect_message(label, exc))
            break
        except SourceRateLimited as exc:
            conn.rollback()
            entry["attempts"] = max(0, int(entry.get("attempts") or 1) - 1)    # not the meeting's fault
            wait = timedelta(seconds=exc.retry_after) if exc.retry_after else RATE_LIMIT_DEFAULT
            state["rate_limited_until"] = _iso(moment + wait)
            break
        except Exception as exc:
            conn.rollback()
            error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("recorder %s of %s: meeting %s failed: %s", kind, owner, ext_id, type(exc).__name__)
            entry.update(status="failed", error=error)
            result["errors"].append({"id": ext_id, "error": error})
    state["recent"] = _recent_list(recent)
    state["cursor"] = adapter.cursor
    stamp = _iso(moment)
    if stop:
        return _fail(conn, row, moment, stop[1], status=stop[0], state=state)
    error = (f"{len(result['errors'])} meeting(s) failed: {result['errors'][0]['error']}"[:1000]
             if result["errors"] else None)
    if wait:
        error = f"{label} is rate limiting this account; the rest waits until {state['rate_limited_until']}"
    _record(conn, row, status="active", state=state, last_poll_at=stamp, last_ok_at=stamp, failures=0,
            last_error=error, next_poll_at=_iso(moment + max(interval(kind), wait or timedelta(0))))
    conn.commit()
    return result


def poll_user(conn, moment: Optional[datetime] = None, force: bool = False, only: Optional[str] = None) -> dict:
    """Every due connection of the ACTING user, each on its own: one failing never stops the next.
    {kind: summary}. only: one connection id (the "Import now" button); force: ignore next_poll_at."""
    moment = moment or datetime.now(timezone.utc)
    results = {}
    for row in list_mine(conn):
        if only and row["id"] != only:
            continue
        if row["status"] == "disconnected" or row["kind"] not in allowed_kinds():
            continue
        if not force and not due(row, moment):
            continue
        if force and row["status"] != "active":
            continue
        try:
            r = poll_connection(conn, row, moment)
        except Exception as exc:                          # never let one connection take the round down
            log.exception("recorder %s: poll failed", row["kind"])
            try:
                conn.rollback()
            except Exception:
                pass
            r = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        results[row["kind"]] = ({"error": r["error"], "status": r.get("status")} if "error" in r else
                                {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
    return results


# ---- the webhook -----------------------------------------------------------------------------------

def owner_of(conn, connection_id: str) -> Optional[str]:
    """The owner of a live connection, with nobody bound: the webhook's first step. None for an unknown,
    malformed or disconnected connection, or an owner who is not active."""
    if not ID_RE.fullmatch(str(connection_id or "")):
        return None
    with conn.as_system():
        if conn.dialect == "postgres":
            return conn.execute("SELECT app_source_connection_owner(?)", (connection_id,)).fetchone()[0]
        row = conn.execute("SELECT owner_id FROM source_connections WHERE id=? AND status<>'disconnected'",
                           (connection_id,)).fetchone()
        if row is None:
            return None
        user = conn.execute("SELECT status FROM users WHERE id=?", (row["owner_id"],)).fetchone()
    if user is not None and user["status"] != "active":
        return None
    return row["owner_id"]


def verify_webhook(row: dict, headers: dict, body: bytes) -> bool:
    """The recorder's own signature where it signs and the rep stored the signing secret; else the
    per-connection token header. Nothing configured: nothing passes."""
    if row.get("webhook_secret_enc") and signatures.has_standard_headers(headers):
        try:
            return signatures.standard_ok(_signing_secret(row), headers, body)
        except tokens.TokenError:
            return False
    return signatures.token_ok(headers.get(signatures.TOKEN_HEADER), row.get("webhook_token_hash"))


def _remember(conn, row: dict, ext_id: str, source_ref: str, **fields) -> None:
    state = dict(row.get("state") or {})
    recent = {e["ext_id"]: e for e in state.get("recent") or [] if isinstance(e, dict) and e.get("ext_id")}
    entry = recent.setdefault(ext_id, {"ext_id": ext_id, "status": "new", "attempts": 0, "source_ref": source_ref})
    entry.update(fields)
    state["recent"] = _recent_list(recent)
    _record(conn, row, state=state)
    conn.commit()


def handle_webhook(db_path, connection_id: str, headers: dict, body: bytes) -> tuple:
    """(HTTP status, JSON body) for one push. The owner is the connection's, found before anything in the
    payload is read; the payload names at most a meeting, which is fetched with the owner's own key."""
    from ..store import stores
    if not identity.cloud():
        return 404, {"error": "per-connection webhooks exist on a cloud install only; use /import/webhook"}
    with identity.activate(None):
        conn = stores.sales(db_path)
    try:
        owner = owner_of(conn, connection_id)
        if owner is None:
            return 404, {"error": "no such connection"}
        with identity.as_user(conn, owner, mode=identity.SERVICE):
            row = get(conn, connection_id)
            if row is None or row["status"] == "disconnected" or row["kind"] not in allowed_kinds():
                return 404, {"error": "no such connection"}
            if not verify_webhook(row, headers, body):
                return 403, {"error": "wrong or missing signature or X-Salescoach-Secret for this connection"}
            try:
                payload = json.loads(body.decode("utf-8", "replace") or "null")
            except ValueError:
                return 422, {"error": "the body is not JSON"}
            try:
                adapter = adapter_for(row)
                ext_id, nt = adapter.webhook_event(payload)
                if not ext_id:
                    return 422, {"error": "the payload names no meeting this recorder knows"}
                source_ref = adapter.source_ref(ext_id)
                found = base.existing_call(conn, source_ref)
                if found:
                    return 200, {"call_id": found, "created": False, "needs_speaker": False}
                if nt is None:
                    nt = adapter.fetch(ext_id)
                outcome = base.import_normalized(conn, nt, deal_id=base.guess_deal(conn, nt), history=False,
                                                 link="account", owner=owner)
            except SourceNotFound:
                conn.rollback()
                _remember(conn, row, ext_id, adapter.source_ref(ext_id), status="pending")
                return 202, {"accepted": True, "note": "the transcript is not ready yet; the next poll brings it in"}
            except (SourceError, tokens.TokenError, ValueError) as exc:
                conn.rollback()
                return 422, {"error": str(exc)[:300]}
            _remember(conn, row, ext_id, nt.source_ref, title=(nt.title or "")[:300], started_at=nt.started_at,
                      status="needs_speaker" if outcome.needs_speaker else "imported", call_id=outcome.call_id)
            return (201 if outcome.created else 200), {"call_id": outcome.call_id, "created": outcome.created,
                                                       "needs_speaker": outcome.needs_speaker}
    finally:
        conn.close()


# ---- "My meetings" --------------------------------------------------------------------------------

MATCH_WINDOW = timedelta(minutes=20)
PAST_DAYS, AHEAD_DAYS = 14, 7


def _emails(raw) -> set:
    try:
        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        items = []
    out = set()
    for item in items if isinstance(items, list) else []:
        email = item.get("email") if isinstance(item, dict) else item
        if email and "@" in str(email):
            out.add(str(email).strip().lower())
    return out


def _near(a: Optional[datetime], b: Optional[datetime]) -> bool:
    return bool(a and b and abs(a - b) <= MATCH_WINDOW)


def meetings(conn, moment: Optional[datetime] = None) -> dict:
    """The acting rep's recent and upcoming meetings for /me/meetings, read-only: their calendar_meetings
    (Phase 5 fills them from Google Calendar; a local connector or nothing before that) left-joined with
    the calls they imported and with what their recorders listed. Each with a status:
      upcoming | imported (call_id) | recorded (listed, not imported yet) | not_recorded
    A recorder meeting with no calendar entry is listed too. Everything is the acting user's own, by an
    explicit owner filter as well as the row-level policies."""
    moment = moment or datetime.now(timezone.utc)
    owner = _owner(conn)
    lo, hi = moment - timedelta(days=PAST_DAYS), moment + timedelta(days=AHEAD_DAYS)
    calendar = [dict(r) for r in conn.execute(
        "SELECT event_id, title, start_at, end_at, attendees, deal_id FROM calendar_meetings WHERE owner_id=? "
        "AND start_at IS NOT NULL ORDER BY start_at", (owner,)).fetchall()]
    calls = [dict(r) for r in conn.execute(
        "SELECT node_id, title, started_at, source, source_ref, wf_state FROM calls WHERE owner_id=? "
        "AND started_at IS NOT NULL ORDER BY started_at", (owner,)).fetchall()]
    calls = [c for c in calls if (_ts(c["started_at"]) or lo) >= lo]
    by_ref = {c["source_ref"]: c for c in calls if c.get("source_ref")}
    listed = []
    for row in list_mine(conn):
        for entry in (row.get("state") or {}).get("recent") or []:
            if isinstance(entry, dict):
                listed.append({**entry, "kind": row["kind"], "label": row.get("label") or row["kind"]})
    used_calls, used_listed = set(), set()
    upcoming, recent = [], []
    for m in calendar:
        start = _ts(m["start_at"])
        if start is None or not (lo <= start <= hi):
            continue
        item = {"title": m["title"] or "Untitled meeting", "start": start, "attendees": sorted(_emails(m["attendees"])),
                "calendar": True, "recorder": None, "call_id": None}
        if start > moment:
            item["status"] = "upcoming"
            upcoming.append(item)
            continue
        guests = set(item["attendees"])
        rec = next((e for e in listed if id(e) not in used_listed and _near(start, _ts(e.get("started_at")))
                    and (not guests or not e.get("emails") or guests & set(e.get("emails") or []))), None)
        call = None
        if rec is not None:
            used_listed.add(id(rec))
            item["recorder"] = rec["label"]
            call = by_ref.get(rec.get("source_ref")) or (
                {"node_id": rec["call_id"]} if rec.get("call_id") else None)
        if call is None:
            call = next((c for c in calls if c["node_id"] not in used_calls and _near(start, _ts(c["started_at"]))), None)
        if call is not None:
            used_calls.add(call["node_id"])
            item.update(status="imported", call_id=call["node_id"])
        elif rec is not None:
            item.update(status="recorded", note=_note(rec))
        else:
            item["status"] = "not_recorded"
        recent.append(item)
    for e in listed:                                   # recorded meetings the calendar does not show
        if id(e) in used_listed:
            continue
        start = _ts(e.get("started_at"))
        if start is not None and start < lo:
            continue
        call = by_ref.get(e.get("source_ref"))
        call_id = call["node_id"] if call else (e.get("call_id") if e.get("status") in ("imported", "needs_speaker") else None)
        if call_id:
            used_calls.add(call_id)
        recent.append({"title": e.get("title") or "Untitled meeting", "start": start, "attendees": e.get("emails") or [],
                       "calendar": False, "recorder": e["label"], "call_id": call_id,
                       "status": "imported" if call_id else "recorded", "note": None if call_id else _note(e)})
    recorder_kinds = set(kinds())
    for c in calls:                                    # imported from a recorder, listing since aged out
        if c["node_id"] in used_calls or c["source"] not in recorder_kinds:
            continue
        recent.append({"title": c["title"] or "Untitled call", "start": _ts(c["started_at"]), "attendees": [],
                       "calendar": False, "recorder": kinds()[c["source"]].label, "call_id": c["node_id"],
                       "status": "imported"})
    far = datetime.min.replace(tzinfo=timezone.utc)
    recent.sort(key=lambda i: i["start"] or far, reverse=True)
    return {"upcoming": upcoming, "recent": recent}


def _note(entry: dict) -> Optional[str]:
    status = entry.get("status")
    if status == "internal":
        return "Only colleagues were on it, so it is not treated as a sales call."
    if status == "pending":
        return "The recorder has not finished the transcript yet; the next check brings it in."
    if status == "failed":
        return f"Import failed: {entry.get('error') or 'unknown error'}"
    return "It comes in at the next check."


def card_context(conn) -> dict:
    """The /me/setup "Your call recorder" cards: one per allowed kind, with the acting user's own
    connection (public fields only; never a key)."""
    mine = {r["kind"]: public(r) for r in list_mine(conn)}
    cards = []
    for d in catalog():
        if not d["allowed"]:
            continue
        row = mine.get(d["kind"])
        state = (row or {}).get("state") or {}
        imported = [e for e in state.get("recent") or [] if isinstance(e, dict) and e.get("status") in ("imported", "needs_speaker")]
        cards.append({**d, "conn": row, "account": state.get("account") or {},
                      "last_imported": max((e.get("started_at") or "" for e in imported), default=None) or None,
                      "rate_limited_until": state.get("rate_limited_until")})
    disallowed = [dict(public(r), label=r.get("label") or r["kind"]) for k, r in mine.items()
                  if k not in allowed_kinds() and r["status"] != "disconnected"]
    return {"recorder_cards": cards, "recorder_disallowed": disallowed,
            "recorder_keys_ready": tokens.keys_configured()}
