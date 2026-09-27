"""Serverless deployment (Vercel): what differs when there are no long-lived processes.

A Vercel deployment runs the web app as functions: any number of short-lived instances, each serving
requests and nothing else, frozen between requests, with a read-only filesystem except /tmp. So:

  * no worker, scheduler, heartbeat, poller or live hub thread starts (the ASGI entrypoint is
    salescoach/vercel_app.py, which builds the `web` role with start_worker=False);
  * the worker and the scheduler become two cron endpoints (salescoach/cron.py, served by the CronDoor in
    vercel_app.py): Vercel Cron calls them with `Authorization: Bearer $CRON_SECRET`; each call drains the bus
    for a bounded time, or runs the scheduler duties that are due, under the same locks the processes use;
  * whatever must survive across requests and instances lives in Postgres (sign-in state and the sign-in rate
    limits: googleauth.StorePending, hosted.StoreLimiter);
  * files go under /tmp (DATA_DIR, RUNTIME_DIR default there); audio upload (needs ffmpeg) and the live coach
    (an in-process hub) are off, and uploads are capped below the platform's request body limit.

Selected by SALESCOACH_PLATFORM=vercel, or by VERCEL=1 (which Vercel sets in every build and function) when
SALESCOACH_PLATFORM is unset. SALESCOACH_PLATFORM=container (or anything else) says "not serverless" even
where VERCEL=1 is set. Nothing here changes a laptop, a container or the Render/Fly deploy.
"""
import hmac
import os
from typing import Optional

PLATFORM_ENV = "SALESCOACH_PLATFORM"
VERCEL = "vercel"
CRON_SECRET_ENV = "CRON_SECRET"
CRON_SECRET_MIN = 16                      # a shorter secret is treated as unset: the cron endpoints refuse
DATABASE_URL_ENV = "SALESCOACH_DATABASE_URL"

# Vercel: "The maximum payload size for the request body or the response body of a Vercel Function is 4.5 MB"
# (vercel.com/docs/functions/limitations). A multipart upload carries the file plus its form fields and
# boundaries, so the file itself must stay a little under it.
BODY_LIMIT_BYTES = 4_500_000
MULTIPART_HEADROOM_BYTES = 64 * 1024
TMP_ROOT = "/tmp/salescoach"

# What a Vercel Marketplace database (Neon) injects: the OWNER role's URLs and parts. A serving function must
# never connect as the owner (row-level security would not apply), and libpq reads PG* variables as defaults,
# so on Vercel they are removed from the process environment before anything connects (scrub_owner_env).
OWNER_ENV = ("DATABASE_URL_UNPOOLED", "DATABASE_MIGRATE_URL", "POSTGRES_URL", "POSTGRES_URL_NON_POOLING",
             "POSTGRES_URL_NO_SSL", "POSTGRES_PRISMA_URL", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST",
             "POSTGRES_DATABASE", "PGHOST", "PGHOST_UNPOOLED", "PGUSER", "PGPASSWORD", "PGDATABASE")

LIVE_OFF = ("The live coach is not available on this deployment: it needs one long-running server process "
            "(its stream and replays live in that process's memory). Calls still get their coaching report.")
AUDIO_OFF = ("Recording upload is not available on this deployment: it needs ffmpeg and a long-running worker. "
             "Connect your call recorder on your profile page (You), or import the transcript as a file or "
             "pasted text below.")


def platform() -> str:
    raw = (os.environ.get(PLATFORM_ENV) or "").strip().lower()
    if raw:
        return raw
    return VERCEL if (os.environ.get("VERCEL") or "").strip() == "1" else "container"


def vercel() -> bool:
    return platform() == VERCEL


# ---- the cron endpoints' credential -------------------------------------------------------------------

def cron_secret() -> Optional[str]:
    raw = (os.environ.get(CRON_SECRET_ENV) or "").strip()
    return raw if len(raw) >= CRON_SECRET_MIN else None


def cron_authorized(header: Optional[str]) -> bool:
    """`Authorization: Bearer <CRON_SECRET>`, compared in constant time. False when the secret is unset (or too
    short to be one): an endpoint with no secret configured refuses everyone rather than no one."""
    secret = cron_secret()
    if secret is None or not header:
        return False
    return hmac.compare_digest(header.strip().encode(), f"Bearer {secret}".encode())


# ---- files --------------------------------------------------------------------------------------------

def default_data_dir() -> Optional[str]:
    """Where SALESCOACH_DATA points when unset: /tmp on Vercel (the bundle is read-only), None elsewhere
    (config.py keeps its own default)."""
    return f"{TMP_ROOT}/data" if vercel() else None


def default_runtime_dir() -> Optional[str]:
    return f"{TMP_ROOT}/runtime" if vercel() else None


# ---- request bodies -----------------------------------------------------------------------------------

def body_limit(configured: int) -> int:
    """An upload route's cap on this platform: the configured one, and on Vercel never more than the platform
    lets through (so the app says "too large, the limit is N MB" instead of the platform's bare 413)."""
    if not vercel():
        return configured
    return min(configured, BODY_LIMIT_BYTES - MULTIPART_HEADROOM_BYTES)


# ---- the database ---------------------------------------------------------------------------------------

def scrub_owner_env() -> list:
    """Vercel: drop the owner role's connection details the Marketplace integration injects, so nothing in a
    serving function can reach the database as the owner (migrations run from an operator's shell or the
    guarded build step, never from a function). DATABASE_URL itself goes too when SALESCOACH_DATABASE_URL
    names the app role. Returns the names removed (never their values)."""
    if not vercel():
        return []
    gone = []
    names = list(OWNER_ENV)
    if (os.environ.get(DATABASE_URL_ENV) or "").strip():
        names.append("DATABASE_URL")
    for name in names:
        if name in os.environ:
            os.environ.pop(name, None)
            gone.append(name)
    return gone


def pooled_url(url: str) -> bool:
    """A connection-pooler URL (Neon's `-pooler` host: PgBouncer in transaction mode). The app binds the acting
    user with a session-level setting for its autocommit reads and holds session advisory locks (the bus's
    per-owner locks, the scheduler's leader lock); a transaction pooler would hand those to another client."""
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    first = host.split(".", 1)[0]
    return first.endswith("-pooler") or "pgbouncer" in host


POOLED = ("{name} points at a connection pooler ({host}): the app needs a direct connection (it binds the acting "
          "user per session and holds session advisory locks, which a transaction-mode pooler shares between "
          "clients). Use the database's direct (unpooled) host, e.g. Neon's DATABASE_URL_UNPOOLED host, with the "
          "app role")


def pooled_problem(url: str, name: str) -> Optional[str]:
    from urllib.parse import urlsplit
    if not pooled_url(url):
        return None
    return POOLED.format(name=name, host=urlsplit(url).hostname)


def startup_problems() -> list:
    """Everything `salescoach serve` checks before a cloud web process starts, for a function's cold start:
    endpoint overrides, the mode, Postgres, the cloud settings (session secret, Google client, token keys), the
    serving role (not one row-level security skips), a pooled URL, and the cron secret. Empty = serve.
    Messages name what is wrong and never a secret's value."""
    from . import cli, endpoints, identity
    from .store import db, stores
    out = list(endpoints.problems())
    raw_mode = (os.environ.get(identity.MODE_ENV) or "").strip().lower()
    if raw_mode != "cloud":
        out.append(f"{identity.MODE_ENV} must be cloud on Vercel (a laptop's single-seller mode keeps its data in "
                   "a SQLite file, and a function's /tmp does not survive the instance)")
        return out
    url = stores.db_path()
    if not db.is_postgres_url(str(url)):
        out.append(stores.CLOUD_NEEDS_POSTGRES + f" (on Vercel: {DATABASE_URL_ENV}, the app role's direct URL)")
        return out
    out.extend(cli.cloud_problems("web"))
    if cron_secret() is None:
        out.append(f"{CRON_SECRET_ENV} is not set (at least {CRON_SECRET_MIN} characters): the cron endpoints refuse "
                   "every call, so no call would ever be analysed and no duty would run")
    pooled = pooled_problem(str(url), DATABASE_URL_ENV if os.environ.get(DATABASE_URL_ENV) else "DATABASE_URL")
    if pooled:
        out.append(pooled)
        return out
    if out:
        return out
    return cli.serving_role_problems(str(url))
