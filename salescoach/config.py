"""Configuration: tracked defaults in config/, the user's own settings on top, secrets apart.

Two layers:
  * config/<name>.yaml is tracked and generic: what every install starts from.
  * user_dir()/<name>.yaml is this install's own (who the seller is, their style guide, their
    accounts, any override). It lives under data/, so it is gitignored, and it wins.

load(name) deep-merges the two. Dicts merge key by key; a list or a scalar in the user file REPLACES
the tracked one (a user who lists two own_domains does not want the shipped ones appended).

Secrets: the environment first, then user_dir()/secrets.env, then the legacy ROOT/secrets.env.
secrets.env follows the house convention (KEY="value" lines, no python-dotenv). A secret's value is
never logged, never put in an exception and never returned by set_secret.

A user file that cannot be read (a YAML typo, a permission) is NOT an error the app dies of (review
3): load() falls back to the tracked defaults for that file, load_user() to nothing, and the problem
(file, line, message) is on record in user_problems() for the banner on /setup and Today. A TRACKED
file that fails to parse is a broken install and still raises.

Cloud mode (SALESCOACH_MODE=cloud with a Postgres store): the overlay lives in the `org_settings`
table instead of the yaml files, one row per name, `body` the same shape the file would have and a
`version` that every save bumps. load() asks the database for the row's version on every call and
caches the merged result by (name, version, tracked file stamp), so three processes (web, worker,
scheduler) all see a change on their next read with no restart and no file to share; a miss costs one
more query for the body. save_user() writes the row; load_user() reads it; user_problems() is empty
(there is no file to be broken). text() / save_user_text() (style.md) and user_file() (accounts.yaml)
stay on disk: the per-user style is in users.style since Phase 1, and secrets stay in the environment
or secrets.env, never in the table.
"""
import json
import logging
import os
import tempfile
import threading
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("salescoach.config")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("SALESCOACH_CONFIG", ROOT / "config"))
DATA_DIR = Path(os.environ.get("SALESCOACH_DATA", ROOT / "data"))
# Not under ~/.claude: the model sandboxes run `claude -p` with this as cwd, and a CLAUDE.md in an
# ancestor directory (~/.claude/CLAUDE.md) would be project memory the agents must never see.
RUNTIME_DIR = Path(os.environ.get("SALESCOACH_RUNTIME",
                                  Path.home() / "Library" / "Application Support" / "salescoach" / "runtime"))
SECRETS_FILE = ROOT / "secrets.env"            # legacy location, read-only; set_secret never writes here


def user_dir() -> Path:
    """This install's own settings. Resolved on every call: tests (and SALESCOACH_SETTINGS) move it."""
    override = os.environ.get("SALESCOACH_SETTINGS")
    return Path(override) if override else Path(DATA_DIR) / "settings"


# ---------------------------------------------------------------------------------------- yaml

UNREADABLE = -2          # a stamp: the file (or its folder) is there but cannot be stat'ed


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except (FileNotFoundError, NotADirectoryError):
        return -1
    except OSError:
        return UNREADABLE


def deep_merge(base, over):
    """`over` on top of `base`. Only dicts merge; anything else in `over` replaces."""
    if not isinstance(base, dict) or not isinstance(over, dict):
        return over
    out = dict(base)
    for key, value in over.items():
        out[key] = deep_merge(base[key], value) if key in base else value
    return out


def load(name: str) -> dict:
    """config/<name>.yaml with the user's <name>.yaml merged over it, cached per version of BOTH files:
    an edit (auto-send off, a new policy, a saved profile) is honoured on the next read, no restart.
    Cloud mode: the org_settings row is the overlay, cached by its version (module docstring)."""
    tracked, user = Path(CONFIG_DIR) / f"{name}.yaml", user_dir() / f"{name}.yaml"
    url = _org_store()
    if url is not None:
        return _load_org(url, name, str(tracked), _stamp(tracked))
    return _load_cached(str(tracked), _stamp(tracked), str(user), _stamp(user))


# ------------------------------------------------------------------------- org settings (cloud)

_org_lock = threading.Lock()
_org_cache: dict = {}          # name -> ((version, tracked_stamp), merged dict)


def _org_store() -> Optional[str]:
    """The Postgres URL when settings live in the database (cloud mode with a Postgres store), else None
    (local mode; or cloud mode pointed at a file, which stores.sales() refuses anyway)."""
    from . import identity
    if not identity.cloud():
        return None
    from .store import db, stores
    target = stores.db_path()
    return target if isinstance(target, str) and db.is_postgres_url(target) else None


def _org_conn(url: str):
    """A pooled connection with the right search_path, bound to whoever is acting (possibly nobody).
    org_settings is SYSTEM: any connection of the app role reads it (config.load runs before anyone is
    bound: the scheduler's intervals, the sign-in page), only an active admin writes it (store/rls.py)."""
    from . import identity
    from .store import stores
    conn, _first = stores._postgres(url)
    identity.bind(conn, identity.current_actor(required=False))
    return conn


def _org_version(url: str, name: str) -> Optional[int]:
    conn = _org_conn(url)
    try:
        with conn.as_system():                  # a settings read may run with nobody bound, on purpose
            row = conn.execute("SELECT version FROM org_settings WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    return int(row["version"]) if row is not None else None


def _org_body(url: str, name: str) -> tuple[Optional[int], dict]:
    conn = _org_conn(url)
    try:
        with conn.as_system():
            row = conn.execute("SELECT version, body FROM org_settings WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, {}
    try:
        body = json.loads(row["body"] or "{}")
    except ValueError:
        log.warning("org_settings %s holds invalid JSON; using the defaults", name)
        body = {}
    return int(row["version"]), body if isinstance(body, dict) else {}


def _load_org(url: str, name: str, tracked: str, tracked_stamp: int) -> dict:
    version = _org_version(url, name)
    key = (version, tracked_stamp)
    with _org_lock:
        hit = _org_cache.get(name)
    if hit is not None and hit[0] == key:
        return hit[1]
    stored, body = _org_body(url, name) if version is not None else (None, {})
    base = _read_yaml(tracked) if tracked_stamp >= 0 else {}
    merged = deep_merge(base, body)
    with _org_lock:
        _org_cache[name] = ((stored, tracked_stamp), merged)
    return merged


def _save_org(url: str, name: str, data: dict) -> None:
    from . import identity
    from .store.stores import now
    actor = identity.current_actor(required=False)
    conn = _org_conn(url)
    try:
        conn.execute(
            "INSERT INTO org_settings(name,body,version,updated_at,updated_by) VALUES (?,?,1,?,?) "
            "ON CONFLICT(name) DO UPDATE SET body=excluded.body, version=org_settings.version+1, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (name, json.dumps(data, ensure_ascii=False, default=str), now(), actor.user_id if actor else None))
        conn.commit()
    finally:
        conn.close()
    with _org_lock:
        _org_cache.pop(name, None)


def org_settings_versions() -> dict:
    """{name: version} of every stored overlay (cloud mode), for tests and support; {} locally."""
    url = _org_store()
    if url is None:
        return {}
    conn = _org_conn(url)
    try:
        with conn.as_system():
            return {r["name"]: int(r["version"]) for r in conn.execute("SELECT name, version FROM org_settings").fetchall()}
    finally:
        conn.close()


@lru_cache(maxsize=256)
def _load_cached(tracked: str, tracked_stamp: int, user: str, user_stamp: int) -> dict:
    base = _read_yaml(tracked) if tracked_stamp >= 0 else {}      # tracked: a failure here raises
    over = _user_yaml(user, user_stamp)
    return deep_merge(base, over)


def _read_yaml(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=256)
def _user_problem(path: str, stamp: int):
    """None, or {file, line, message} for this version of a user file that cannot be read."""
    if stamp == -1:
        return None
    if stamp == UNREADABLE:
        return {"file": path, "line": None, "message": "the file cannot be read (permissions?)"}
    try:
        _read_yaml(path)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else None
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return {"file": path, "line": line, "message": str(problem)}
    except OSError as exc:
        return {"file": path, "line": None, "message": f"the file cannot be read ({type(exc).__name__})"}
    return None


def _user_yaml(path: str, stamp: int) -> dict:
    if stamp == -1:
        return {}
    problem = _user_problem(path, stamp)
    if problem:
        log.warning("%s could not be read (%s%s); using the defaults", path,
                    f"line {problem['line']}: " if problem["line"] else "", problem["message"])
        return {}
    return _read_yaml(path)


def load_user(name: str) -> dict:
    """Only what the user saved for <name> (no tracked defaults). For settings forms. A file that
    cannot be read counts as nothing saved (and is on record in user_problems()). Cloud: the row."""
    url = _org_store()
    if url is not None:
        return _org_body(url, name)[1]
    path = user_dir() / f"{name}.yaml"
    return _user_yaml(str(path), _stamp(path))


def user_problems() -> list:
    """Every user settings file that cannot be read right now: [{file, name, line, message}], for the
    banner. Checked against the files' current versions, so a fixed file drops off at once."""
    out = []
    if _org_store() is not None:
        return out                                  # rows, not files: nothing to be unreadable
    try:
        paths = sorted(user_dir().glob("*.yaml"))
    except OSError:
        return [{"file": str(user_dir()), "name": "settings folder", "line": None,
                 "message": "the settings folder cannot be read (permissions?)"}]
    for path in paths:
        problem = _user_problem(str(path), _stamp(path))
        if problem:
            out.append({**problem, "name": path.name})
    return out


def _atomic_write(path: Path, data: str, mode: int | None = None):
    """Write beside the target, then rename over it: a reader sees the old file or the new, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Dumper(yaml.SafeDumper):
    pass


# A multi-line value (a signature) as a literal block: the user's files are meant to be read and edited.
_Dumper.add_representer(str, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None))


def save_user(name: str, data: dict) -> Optional[Path]:
    """Replace the user's <name>.yaml. The tracked file is never written. A file that could not be
    read is kept beside the new one as <name>.yaml.broken-<stamp>: a save must not destroy hand edits.
    Cloud mode: the org_settings row instead (its version bumps; every process sees it next read);
    returns None, there is no path."""
    if not isinstance(data, dict):
        raise TypeError("settings must be a mapping")
    url = _org_store()
    if url is not None:
        _save_org(url, name, data)
        return None
    path = user_dir() / f"{name}.yaml"
    if _user_problem(str(path), _stamp(path)):
        try:
            os.replace(path, path.with_name(f"{path.name}.broken-{_stamp(path)}"))
        except OSError:
            pass
    _atomic_write(path, yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                                  default_flow_style=False, width=110))
    _load_cached.cache_clear()          # two saves inside one mtime tick must not serve the first
    _user_problem.cache_clear()
    return path


def text(name: str) -> str:
    """A text setting (style.md): the user's file when there is one and it can be read, else the
    tracked default."""
    own = user_dir() / name
    try:
        if own.is_file():
            return own.read_text()
    except OSError as exc:
        log.warning("%s cannot be read (%s); using the tracked %s", own, type(exc).__name__, name)
    tracked = Path(CONFIG_DIR) / name
    return tracked.read_text() if tracked.exists() else ""


def save_user_text(name: str, content: str) -> Path:
    path = user_dir() / name
    _atomic_write(path, content)
    return path


def user_file(name: str) -> Path:
    """The user's copy of a config file if it exists, else the tracked one (accounts.yaml)."""
    own = user_dir() / name
    return own if own.exists() else Path(CONFIG_DIR) / name


# ------------------------------------------------------------------------------------- secrets

def _parse_env(path: Path) -> dict:
    out = {}
    try:
        lines = path.read_text().splitlines()
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _user_secrets_file() -> Path:
    return user_dir() / "secrets.env"


def secret(key: str, default=None):
    if key in os.environ:
        return os.environ[key]
    for path in (_user_secrets_file(), SECRETS_FILE):
        values = _parse_env(path)
        if key in values:
            return values[key]
    return default


def has_secret(key: str) -> bool:
    return bool(secret(key))


def set_secret(key: str, value: str) -> None:
    """Store one secret in user_dir()/secrets.env (0600, atomic). Returns nothing and says nothing
    about the value: errors name the key only."""
    key = str(key or "").strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        raise ValueError("secret name must be letters, digits and underscores")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key}: empty value")
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError(f"{key}: a secret cannot contain a line break")
    if value != value.strip() or value[0] in "\"'" or value[-1] in "\"'" or ('"' in value and "'" in value):
        raise ValueError(f"{key}: a secret cannot start or end with a space or a quote")
    path = _user_secrets_file()
    quote = "'" if '"' in value else '"'
    new_line = f"{key}={quote}{value}{quote}"
    try:
        lines = path.read_text().splitlines()
    except (FileNotFoundError, NotADirectoryError):
        lines = []
    # Only the line for THIS key changes; comments, blank lines and every other line stay as they are.
    out, done = [], False
    for line in lines:
        text = line.strip()
        if text and not text.startswith("#") and "=" in text and text.partition("=")[0].strip() == key:
            if not done:
                out.append(new_line)
                done = True
            continue                                   # a second line for the same key is dropped
        out.append(line)
    if not done:
        out.append(new_line)
    _atomic_write(path, "\n".join(out) + "\n", mode=0o600)
    os.chmod(path, 0o600)


def calls_dir() -> Path:
    path = DATA_DIR / "calls"
    path.mkdir(parents=True, exist_ok=True)
    return path
