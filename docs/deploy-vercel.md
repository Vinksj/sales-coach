# Deploying Sales Coach to Vercel (team install)

This is the team install of [deploy-cloud.md](deploy-cloud.md) (`SALESCOACH_MODE=cloud`: Postgres with row-level
security, Google sign-in, each rep's own Gmail, Calendar and recorder) on Vercel instead of a container host. Read
deploy-cloud.md first: the Google Workspace checklist, the roles, sign-in, consent, offboarding, backups and the
launch checklist are the same. This page is what differs, and the steps for a person deploying to their own Vercel
account. The container deploy (Docker, Render, Fly: `salescoach serve --role web|worker|scheduler`) and the laptop
install are unchanged; Vercel is an additional target, chosen by the platform (`VERCEL=1`, which Vercel sets, or
`SALESCOACH_PLATFORM=vercel`).

## How it runs on Vercel

A Vercel deployment has no long-lived process: the app runs as functions, any number of short-lived instances,
frozen between requests, with a read-only filesystem except `/tmp`. So:

| On a container host | On Vercel |
|---|---|
| `web` processes | One Python function serving `salescoach.vercel_app:app` (`[tool.vercel] entrypoint` in pyproject.toml). At cold start it runs the same checks `salescoach serve` runs in cloud mode, plus two of its own (a pooler URL, `CRON_SECRET`); with a problem every page is a 500 naming it (and `/health` a 503), and the checks run again every 30 s, so fixing the environment heals it without a redeploy. It starts no thread: no worker, scheduler, heartbeat, poller or live hub. |
| `worker` processes | `GET /cron/drain`, called by Vercel Cron every minute: the worker's own claim / handle / settle loop, claiming for 50 s (`SALESCOACH_CRON_DRAIN_S`), then finishing the event in hand (the function may run 800 s) and returning. What it did not claim waits for the next minute. Overlapping calls behave like two worker loops: the per-owner advisory locks never let one owner's two events, or one event twice, run at once. |
| the `scheduler` process | `GET /cron/tick`, every minute: each duty (follow-ups, reply polling, calendar, retention, recorders, learning, embeddings) that is due runs once, under the same leader lock a scheduler process takes; a second tick meanwhile answers `skipped`. Due times are kept in the database. |
| heartbeats, "worker off" | The last drain and tick are recorded (`/health`: `"platform": "vercel"`, `"worker": "cron"`, `"cron": {...}`); the "worker off" pill appears only when no drain has succeeded for 3 minutes (`SALESCOACH_CRON_STALE_S`). |
| sign-in state in the web process | In Postgres (`auth_pending`, `auth_attempts`): a sign-in started on one instance completes on another, and the sign-in rate limits count every instance's refusals. (A container install uses the same tables now.) |
| `/data` on a volume | `/tmp/salescoach` (nothing there needs to survive: cloud mode keeps everything in the database). |

The two cron endpoints answer only `Authorization: Bearer $CRON_SECRET` (compared in constant time; with
`CRON_SECRET` unset they refuse every call), take `GET` only, and are answered in front of the browser middleware
(same-origin guard, sign-in, first-run redirects), because Vercel Cron calls the deployment's own URL, not your
domain. A container build has no such route.

### What is off on Vercel, and why

- **Recording upload** (Import > Upload a recording). It needs ffmpeg, which the Python runtime does not have, a
  worker that can transcribe for minutes, and a request body larger than Vercel's 4.5 MB. The Import page says so;
  reps connect their recorder (Fathom, Fireflies, tl;dv, Granola) on their profile page, or import the transcript as
  a file or pasted text, both of which work.
- **The live coach's stream and replay** (`/coach/live/stream`, "Replay this call"). Both live in one process's
  memory (the hub, the replay thread), which a function instance does not keep between requests; they answer 404
  with a reason and the Replay card is hidden. The post-call coaching and the nudge timeline still work. Live capture
  and local transcription were already off in any cloud install.
- **Uploads over about 4.2 MB.** Vercel refuses any request body over 4.5 MB before the app sees it, so the upload
  cap is kept under it (`/import/file`, `/import/text`), the page refuses a larger file before sending it, and the
  app answers "Too large: the limit for this upload is 4.2 MB" for one that gets through.

### Limits to know

- **Latency of background work is the cron cadence.** A new call, a redraft or a strategy request waits for the next
  drain: up to a minute, plus the work itself. Vercel's Python runtime has no documented way to keep working after a
  response (`waitUntil` is Node.js-only), so there is no "kick" after a button press; the cron is the worker.
- **One event must finish within the function's duration.** A drain claims for 50 s and the function may run 800 s
  (Pro), so a single event has about 12 minutes. A call's pipeline commits after every step; if an instance is cut
  off mid-event, the next drain returns the event to pending (the dead session released its lock) and it resumes at
  the next step, counting one attempt of three.
- **Vercel Cron is best effort.** A run can be late, missed or duplicated and is not retried; a missed minute is
  picked up by the next. Duplicated or overlapping runs are safe (the locks above).
- **Responses are limited to 4.5 MB too** (Vercel's limit for request and response bodies). "Download my data" streams
  its zip; a very large export may still hit a limit. Operators can always run `salescoach export --user` from
  their own machine.
- **Database connections.** Each instance keeps up to 5 (`SALESCOACH_PG_POOL_SIZE`), checked on checkout and closed
  after a minute idle, and each drain or tick in flight holds one more of its own. Size the Neon compute for your
  instance count.

## Requirements

- **Vercel Pro (or Enterprise).** The two crons run every minute; on Hobby a cron runs at most once a day and a
  deployment with a per-minute schedule fails, and Hobby's longest function duration is 300 s where vercel.json
  asks for 800 s. (<https://vercel.com/docs/cron-jobs/usage-and-pricing>,
  <https://vercel.com/docs/functions/configuring-functions/duration>)
- **Postgres with direct connections and an owner role that bypasses row-level security.** Neon from the Vercel
  Marketplace is what this page uses: a project's owner role (`neondb_owner`) has `CREATEROLE` and, for projects
  created after 2023-08-15, `BYPASSRLS` (<https://neon.com/docs/manage/roles>). Check it in step 3.
- The Google Workspace OAuth client of [deploy-cloud.md](deploy-cloud.md) ("Workspace admin checklist"), a model
  provider key, and a Python 3.11+ with this repository installed on your own machine for the one-time commands.

## Step by step

### 1. Create the project

In Vercel: Add New > Project > import the GitHub repository, choose the branch to deploy as production. Leave the
root directory at the repository root and the build and install commands empty: Vercel detects FastAPI, reads the
entrypoint from `[tool.vercel]` in pyproject.toml, installs `[project].dependencies` (not the `asr` extra: no MLX,
no Whisper), and uses Python 3.12 from `.python-version` (`requires-python` alone made the CLI pick 3.11, which
Vercel's runtime refuses). `vercel.json` sets the function's `maxDuration` and the two crons; `.vercelignore` keeps
the tests, docs and the laptop app out of the upload. Do not deploy yet (or let the first deploy fail on the
missing settings; it serves the problem page, nothing else).

### 2. Add Neon from the Marketplace

Storage (or Integrations) > Marketplace > Neon > create a database and connect it to the project. It adds the
OWNER role's connection details to the project's environment: `DATABASE_URL` (pooled, through PgBouncer),
`DATABASE_URL_UNPOOLED` (direct), `PGHOST`, `PGHOST_UNPOOLED`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` and the legacy
`POSTGRES_URL`, `POSTGRES_URL_NON_POOLING`, `POSTGRES_USER`, `POSTGRES_HOST`, `POSTGRES_PASSWORD`,
`POSTGRES_DATABASE`, `POSTGRES_URL_NO_SSL`, `POSTGRES_PRISMA_URL`
(<https://neon.com/docs/guides/vercel-managed-integration>).

The app never serves as the owner: row-level security would not apply to it (it owns the tables and bypasses RLS).
It serves as `salescoach_app` through `SALESCOACH_DATABASE_URL` (step 6), and at every cold start it removes the
variables above from its own environment, so nothing in a function can reach the database as the owner. Copy the
value of `DATABASE_URL_UNPOOLED` for the next steps (Settings > Environment Variables, or the Neon console's
connection string with "Connection pooling" off).

**Direct, never pooled.** The app binds the acting user with a session-level setting for its reads outside a
transaction and holds session advisory locks (the bus's per-owner locks, the scheduler's lock); PgBouncer in
transaction mode, which is what Neon's `-pooler` host is, runs each transaction on whichever server connection is
free and does not support session advisory locks or `SET` (<https://neon.com/docs/connect/connection-pooling>). So
every URL the app or its commands use is a direct one (the host without `-pooler`); the app refuses to start with a
pooler URL, and `salescoach migrate` and `salescoach app-role` refuse one too.

Preview deployments: Neon's preview branching is off by default. Leave it off; the app's own settings (step 6) are
set for Production only, so a preview deployment shows the "not ready to serve" page and touches no database.

### 3. Check the owner role

```
psql "$OWNER_URL" -c "SELECT rolname, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname = current_user"
```

(`OWNER_URL` = the `DATABASE_URL_UNPOOLED` value.) Both must be `t`. `salescoach app-role` checks the same and
refuses an owner without `BYPASSRLS`: the migrator needs it (it backfills every row; `app_owner_of()` reads rows as
the owner).

### 4. Create the app role

From your own machine, in a clone of the repository (`python -m venv .venv && .venv/bin/pip install -e .`):

```
DATABASE_MIGRATE_URL="$OWNER_URL" .venv/bin/salescoach app-role --vercel
```

It creates `salescoach_app` (LOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, owning nothing, CONNECT on
the database) and prints `SALESCOACH_DATABASE_URL=postgresql://salescoach_app:...@<direct host>/<db>?sslmode=require`
once. Keep it for step 6 and in your password manager. Run again, it checks the role and changes nothing;
`--password-stdin` sets a new password (rotation: update the variable and redeploy).

### 5. First migration

```
DATABASE_MIGRATE_URL="$OWNER_URL" .venv/bin/salescoach migrate
DATABASE_MIGRATE_URL="$OWNER_URL" .venv/bin/salescoach migrate --check      # exits 0: up to date
```

This creates the schema and the row-level policies and grants `salescoach_app` its tables.

### 6. Environment variables

Settings > Environment Variables, each for the **Production** environment only (so no preview build or preview
deployment can read them):

| Variable | Value, and where it comes from |
|---|---|
| `SALESCOACH_MODE` | `cloud` |
| `SALESCOACH_DATABASE_URL` | the app role's direct URL printed in step 4 |
| `SALESCOACH_PUBLIC_URL` | `https://<your domain>`: the production domain people type (Settings > Domains). Host and Origin are checked against it, and it is the base of the OAuth redirect URIs |
| `SALESCOACH_SESSION_SECRET` | a long random string, e.g. `openssl rand -base64 48`; signs the session cookie |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | the Workspace OAuth client ([deploy-cloud.md](deploy-cloud.md), checklist) |
| `GOOGLE_ALLOWED_DOMAINS` | comma list of the Workspace domains whose members may sign in |
| `SALESCOACH_BOOTSTRAP_ADMIN` | the first admin's address (used once) |
| `SALESCOACH_TOKEN_KEYS` | `salescoach tokens new-key` on your machine prints `kid:base64`; the key ring for OAuth tokens and recorder keys |
| `CRON_SECRET` | a random string of at least 16 characters, e.g. `openssl rand -hex 32`. Vercel sends it with every cron call; the app refuses the cron endpoints without it (<https://vercel.com/docs/cron-jobs/manage-cron-jobs>) |
| the model provider's key | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `LLM_API_KEY`, as in [deploy.md](deploy.md); set a spend limit at the provider |
| `LLM_BUDGET_ORG_USD_DAY`, `LLM_BUDGET_USER_USD_DAY` | daily model-spend caps ([deploy-cloud.md](deploy-cloud.md), "Model budgets") |
| optional: `SALESCOACH_CRON_DRAIN_S` | seconds a drain keeps claiming new events (default 50) |
| optional: `SALESCOACH_CRON_TICK_S` | seconds a tick keeps starting duties (default 50; the rest run next minute) |
| optional: `SALESCOACH_CRON_STALE_S` | after how long without a drain the pill says "worker off" (default 180) |
| optional: `SALESCOACH_PG_POOL_SIZE` | connections per instance (default 5 on Vercel) |

Not needed on Vercel: `DATABASE_URL` (the integration's, the owner's: removed at start), `DATABASE_MIGRATE_URL`
(the owner's; only for the opt-in build step below), `SALESCOACH_ROLE`, `WORKER_CONCURRENCY`, `SALESCOACH_DATA`,
`SALESCOACH_RUNTIME` (default under `/tmp`). Never set `SALESCOACH_E2E` or the endpoint overrides
(`GOOGLE_OAUTH_BASE`, `GOOGLE_API_BASE`, `SALESCOACH_FIREFLIES_URL`, `SALESCOACH_FATHOM_BASE`): they exist for the
test harness, and the app refuses to start with one set.

### 7. Google OAuth redirect URIs

In the Google Cloud console, the Web application client: add exactly `https://<your domain>/auth/callback` and
`https://<your domain>/auth/connect/callback`, the domain being `SALESCOACH_PUBLIC_URL`'s. A `*.vercel.app` preview
URL is not one of them, on purpose.

### 8. Deploy

Deploy the production branch (Deployments > Redeploy, or push). Vercel registers the two crons from `vercel.json`
on production deployments only (<https://vercel.com/docs/cron-jobs>): Settings > Cron Jobs lists `/cron/drain` and
`/cron/tick`, every minute.

### 9. The first admin

Open `https://<your domain>`, sign in with Google as `SALESCOACH_BOOTSTRAP_ADMIN`: that address becomes the first
admin, once. Then follow deploy-cloud.md's launch checklist from step 5 (settings, invites, teams, recorders,
consent, retention, budgets, test sign-ins, backups, the security review).

### 10. Verify

- `curl https://<your domain>/health` answers 200 with `"platform": "vercel"`, `"db": "ok"`, and within two minutes
  `"worker": "cron"` and a `"cron"` block whose `drain` and `tick` are recent (`age_s` under 60). No `workers` or
  `schedulers`: there are none.
- `curl -H "Authorization: Bearer $CRON_SECRET" https://<your domain>/cron/drain` answers
  `{"status": "ok", "job": "drain", "handled": ..., "stopped": "empty"}`; without the header, 401.
- Settings > Cron Jobs: "Run" each job once; the function logs show `GET /cron/drain` and `GET /cron/tick` with 200.
- Import a pasted transcript (Import > Paste a transcript): the call page says it is queued, and within about a
  minute (the next drain) it moves through the steps to "Ready for review".
- A rep signs in, connects Gmail and their recorder, and sees a call of theirs arrive; a second rep gets 404 for it
  (deploy-cloud.md, launch checklist 11).
- The Import page shows "Recording upload is not available on this deployment".

## Migrations on upgrades

**Default: by hand, from your machine, before promoting.** For every upgrade that changes the schema (the release
notes say so; `salescoach migrate --check` exits 1 against the new build):

```
DATABASE_MIGRATE_URL="$OWNER_URL" .venv/bin/salescoach migrate
```

then deploy (or promote) the new build. Why this is the default: the owner's credentials never enter Vercel at all,
no preview build can run a migration against production, and a migration is an act somebody chose to take. Every
instance refuses to serve a schema at another version than its own build's (the problem page names it), so between
the migration and the new deployment going live the old one shows that page: migrate and deploy together, in a
quiet moment. An Instant Rollback to an older build after a migration serves the same page until the database is
restored ([deploy-cloud.md](deploy-cloud.md), "Backup and restore").

**Opt-in: on the production build.** If you prefer the build to migrate, add to pyproject.toml

```
[tool.vercel.scripts]
build = "python -m salescoach.cli vercel-build"
```

and set, for the **Production** environment only, `SALESCOACH_MIGRATE_ON_BUILD=1` and `DATABASE_MIGRATE_URL` (the
owner's direct URL). `salescoach vercel-build` migrates only when all three hold (`VERCEL_ENV=production`, the flag,
the URL) and says why it skipped otherwise; a failed migration fails the build, so the deployment is never promoted
onto a half-migrated schema. The owner's URL then lives in Vercel's Production environment, readable by every
production build.

## How it was tested

`e2e/run.sh vercel` runs the whole team install through a local simulation of Vercel's execution model
(`e2e/docker-compose.vercel.yml`): two instances of the image serving only `salescoach.vercel_app:app` under a
plain ASGI server, with a read-only root filesystem and a tmpfs `/tmp`, the owner's Neon-style variables in their
environment and the app role in `SALESCOACH_DATABASE_URL`; a round-robin edge (`e2e/vercel/proxy.py`) that sends each
request to the next instance, refuses bodies over 4.5 MB and sets `X-Forwarded-For`; and a cron caller
(`e2e/vercel/cron.py`) that calls both endpoints with the secret every 5 seconds without waiting for the previous
call. There is no worker or scheduler process. The same end-to-end steps as the container run pass through it
(sign-in, invites, recorders, analysis, isolation, the manager, budgets, Gmail send, offboarding, import-sqlite,
Fathom), plus: a sign-in started on one instance completes on the other; audio upload and the live coach are off and
uploads meet the body limit; a slow event's drain overlaps the next drains and the event still runs once; concurrent
ticks run each duty once. What only a real deployment can show: Vercel's own Python runtime and cold starts, Cron
delivery, the bundle's size handling, and Neon's roles.

`vercel build` (Vercel CLI, no login, nothing deployed) builds the project into one Python 3.12 function with
`maxDuration` 800 and both crons. The dependency set is about 245 MB unpacked for Linux x86_64 (the Google API
client's bundled discovery documents are 100 MB of it), under the Python bundle limit of 500 MB
(<https://vercel.com/docs/functions/runtimes/python>); the CLI reports it as over the "standard size" and
optimises it by installing part of the dependencies when an instance starts, which costs cold-start time.
