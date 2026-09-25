"""Who is acting: the one place the acting user is set, read and bound to a connection.

Identity is org profile + acting user (plan, Approach 3). Everything that used to mean "the
seller" (the is_me row, the profile, the seller's memory) now means "the ACTING user", and the
acting user comes from exactly one place: the Actor held in a contextvar, bound to the store
connection as `conn.actor` and, on Postgres, to the session setting `app.user_id` that the
owner_id defaults (Phase 1) and the row-level policies (Phase 2) read.

  session(user_id, mode=)     open a connection AND set the actor everywhere; the entry point of every
                              thread that has no request context (worker, scheduler, background loops)
  as_user(conn, user_id)      re-bind an existing connection (the worker resolving an event's owner)
  activate(actor)             the contextvar alone, for a thread that opens its own connection later
  current_actor()             the acting user; NoActor when there is none in cloud mode
  bind(conn, actor)           conn.actor + the Postgres setting (what stores.sales() calls on open)

Two modes (SALESCOACH_MODE):
  local   the default. One user, "local", is implicit everywhere: a bare thread, a CLI command, a
          test that never mentions users all act as "local", so the single-user product is unchanged.
  cloud   a hosted, multi-user install on Postgres. There is no implicit user: current_actor() with
          nothing set raises NoActor, which is how a background thread that forgot to open a session
          is caught (a forgotten actor must never silently become somebody's data).

On Postgres the binding is the session settings app.user_id / app.mode, set for the session by
bind() and re-issued transaction-locally at the start of every transaction
(store/db.py PostgresConnection._on_begin), which the row-level policies (store/pg/rls.sql)
read. A connection with no actor sees and writes nothing OWNED; in the test suite it also trips
db.NoActorBound unless the code marked the connection conn.as_system() on purpose.
"""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Optional

LOCAL_USER = "local"
MODE_ENV = "SALESCOACH_MODE"
MODES = ("local", "cloud")
INTERACTIVE, SERVICE = "interactive", "service"


class NoActor(RuntimeError):
    """Cloud mode, and nothing said who is acting. A thread without a session, or a route the auth
    layer did not resolve to a user: fail here, loudly, rather than read or write as nobody."""


def mode() -> str:
    """"local" (default) or "cloud"; anything else is refused at startup (cli) and read as local here."""
    value = (os.environ.get(MODE_ENV) or "local").strip().lower()
    return value if value in MODES else "local"


def cloud() -> bool:
    return mode() == "cloud"


@dataclass(frozen=True)
class Actor:
    user_id: str
    mode: str = INTERACTIVE                 # interactive (a person at the keyboard) | service (a background duty)
    role: str = "rep"                       # rep | manager | admin
    profile: Optional[dict] = field(default=None, compare=False)   # the user's own fields (users row); None = seller.yaml

    @property
    def is_local(self) -> bool:
        return self.user_id == LOCAL_USER

    def as_service(self) -> "Actor":
        return replace(self, mode=SERVICE)


LOCAL_ACTOR = Actor(LOCAL_USER, INTERACTIVE, "admin")

_current: ContextVar = ContextVar("salescoach_actor", default=None)


def current_actor(required: bool = True) -> Optional[Actor]:
    """The acting user. Local mode falls back to the implicit local user; cloud mode raises NoActor
    (or returns None with required=False, for code that only wants to know)."""
    actor = _current.get()
    if actor is not None:
        return actor
    if not cloud():
        return LOCAL_ACTOR
    if required:
        raise NoActor("no acting user: this code ran outside identity.session() in cloud mode")
    return None


def current_user_id() -> str:
    return current_actor().user_id


def actor_of(conn) -> Actor:
    """The actor bound to `conn`, else the current one (which is what bind() would have used)."""
    return getattr(conn, "actor", None) or current_actor()


@contextmanager
def activate(actor: Optional[Actor]):
    """Set the contextvar for the block (None clears it). For a thread that will open its own store."""
    token = _current.set(actor)
    try:
        yield actor
    finally:
        _current.reset(token)


def bind(conn, actor: Optional[Actor]) -> None:
    """Attach `actor` to a connection: conn.actor, and on Postgres the session settings the owner_id
    defaults read. None unbinds (the setting becomes '', which the defaults treat as missing)."""
    conn.bind_actor(actor)


def _load(conn, user_id: str, mode: str, role: Optional[str]) -> Actor:
    if user_id == LOCAL_USER:
        return Actor(LOCAL_USER, mode, role or "admin", None)
    from . import users
    with conn.as_system():                          # reading the row that BECOMES the actor: nobody is bound yet
        row = users.get(conn, user_id)
    if row is None:
        raise NoActor(f"no such user: {user_id}")
    return Actor(user_id, mode, role or row["role"], users.profile_of(row))


@contextmanager
def session(user_id: str, mode: str = INTERACTIVE, role: Optional[str] = None, db_path=None):
    """Open a store connection as `user_id`: the connection, the contextvar and the Postgres setting
    all say so until the block ends, then everything is restored and the connection closed."""
    from .store import stores
    with activate(None):                            # the open must not inherit a stale actor
        conn = stores.sales(db_path)
    try:
        actor = _load(conn, user_id, mode, role)
        with as_actor(conn, actor):
            yield conn
    finally:
        conn.close()


@contextmanager
def as_actor(conn, actor: Optional[Actor]):
    previous = getattr(conn, "actor", None)
    bind(conn, actor)
    try:
        with activate(actor):
            yield conn
    finally:
        bind(conn, previous)


@contextmanager
def as_user(conn, user_id: str, mode: str = INTERACTIVE, role: Optional[str] = None):
    """Run the block on an existing connection as `user_id` (the worker, once it knows an event's
    owner). The previous binding comes back afterwards."""
    with as_actor(conn, _load(conn, user_id, mode, role)):
        yield conn


def refresh(conn) -> Optional[Actor]:
    """Re-read the acting user's row after a profile edit, so the rest of the request sees it."""
    actor = current_actor(required=False)
    if actor is None or actor.is_local:
        return actor
    fresh = _load(conn, actor.user_id, actor.mode, None)
    _current.set(fresh)
    bind(conn, fresh)
    return fresh
