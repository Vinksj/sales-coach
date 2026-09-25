"""Names of the per-test Postgres schemas, and which of them a starting session may drop.

Several pytest sessions may share one database (two worktrees, a suite run while another is going). Each
session names its schemas with its own prefix,

    sc_test_<UTC yyyymmddHHMMSS>_<pid>_<4 hex>_<12 hex per test>

and drops only (1) its own, (2) any whose timestamp is older than STALE_AFTER (a session that old is gone:
the whole suite takes minutes), and (3) names from before this scheme (sc_test_<12 hex>, no timestamp) only
when no other client is connected to the database, since then no session can be using them. A schema of a
session that is running right now is never dropped by another one.
"""
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

BASE = "sc_test_"
STALE_AFTER = timedelta(hours=6)
_STAMP = "%Y%m%d%H%M%S"
_NAME = re.compile(r"^sc_test_(\d{14})_\d+_[0-9a-f]{4}_")
_LEGACY = re.compile(r"^sc_test_[0-9a-f]{12}$")


def session_prefix(now=None, pid=None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{BASE}{now.strftime(_STAMP)}_{os.getpid() if pid is None else pid}_{secrets.token_hex(2)}_"


def created_at(name: str):
    """The UTC time a schema's session started, from its name; None for a name without one."""
    m = _NAME.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), _STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_drop(names, own_prefix: str, now=None, others_connected: bool = True) -> list:
    """Which of `names` (every sc_test_% schema in the database) a session with `own_prefix` may drop."""
    now = now or datetime.now(timezone.utc)
    out = []
    for name in names:
        if not name.startswith(BASE):
            continue
        if name.startswith(own_prefix):
            out.append(name)
            continue
        born = created_at(name)
        if born is not None:
            if now - born > STALE_AFTER:
                out.append(name)
        elif _LEGACY.match(name) and not others_connected:
            out.append(name)
    return out
