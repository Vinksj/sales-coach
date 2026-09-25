# Deploying Sales Coach for a team (cloud mode)

A team install runs with `SALESCOACH_MODE=cloud` on Postgres (`DATABASE_URL`), built from the same
image as the single-seller install in [deploy.md](deploy.md). This page collects what is specific to it.
Each section stands on its own; the phases that wrote them are noted where it matters.

## Processes

One image, three roles, chosen by `salescoach serve --role` or the `SALESCOACH_ROLE` variable the
container's entrypoint passes through:

| Role | Runs | HTTP | Scale |
|---|---|---|---|
| `all` (default) | the web app, one worker loop, every scheduler duty, the embed/learning loops, the heartbeats: today's single process, what a laptop and the one-seller container run | yes | exactly one (SQLite has one writer; on Postgres it would be one of everything) |
| `web` | the web app only. No worker thread, no duties, no Jarvis sync, no embed loop | yes | as many as you like |
| `worker` | `WORKER_CONCURRENCY` (default 2) worker loops, each on its own connection, plus a heartbeat. Nothing else | no | as many as you like |
| `scheduler` | the duties (follow-ups, reply polling, calendar, auto-send, the sources poller, the learning recompute) and the intel embed loop, under a leader election, plus a heartbeat | no | one is enough; two or more elect one leader |

**Leader election.** Every `scheduler` process opens a connection of its own (not from the pool)
and tries `pg_try_advisory_lock(hashtext('salescoach:scheduler:' || current_schema()))`. The holder
starts the duties and refreshes `state['ops:scheduler:leader']` every 15 s; the others write a
heartbeat saying `leader: false` and try again every 5 s. The lock is a session lock: when the holder's
process ends (a deploy, a crash) or its connection drops, Postgres releases it and a standby takes over
on its next try. A leader that finds its lock connection dead stops its duties (a duty in the middle of a
round finishes that round; its thread ends after) and goes back to trying. On SQLite there is no
election: one process per file is assumed, and `salescoach serve` (role `all`) is that process.
Two scheduler processes on SQLite would both run the duties; do not do that.

**Fairness on the bus.** Every workflow event carries a `priority` (interactive requests such as a
redraft, a retry, a strategy, prep or coach-report request publish at 10; new calls, imports and the
daily duties at 0; history backfills at -10) and an `owner`. A
claim takes the highest priority first, then the oldest id, and **skips events whose owner already has
an event running**: on Postgres the claimer takes a session advisory lock on the owner
(`pg_try_advisory_lock(hashtext('salescoach:owner:<id>'))`) before marking the row running and
releases it when the event is settled, so two workers can never both run one owner's events, a rep's
bulk import cannot take every worker while another rep waits, and an owner's events of equal priority
are handled in id order. A deal has one owner, so a deal's events keep their order too (only an
interactive request, priority 10, overtakes a pending import; a methodology switch's re-strategy stays
at 0 for that reason). Pooled connections drop every
advisory lock when they go back to the pool. On SQLite the claim runs under the file's one write lock
and checks running owners with a `NOT EXISTS`, which the write lock makes race-free.

**Heartbeats.** Worker and scheduler processes write a row into the org-wide `state` table every 15 s:

| Key | Body |
|---|---|
| `ops:worker:<hostname>:heartbeat` | `{"at", "pid", "host", "started_at", "concurrency", "handled", "busy"}` |
| `ops:scheduler:<hostname>:heartbeat` | `{"at", "pid", "host", "started_at", "leader"}` |
| `ops:scheduler:leader` | `{"host", "pid", "since", "at"}`, refreshed by the holder |

`GET /health` on a web process answers `{"status", "version", "db", "role", "worker", "processes",
"configured"}` where `processes` lists those rows with `age_s` and `stale` (older than 50 s), so a dead
worker or scheduler is visible from outside: the web tier keeps serving, the nav shows "worker off" and
the queue grows. `salescoach health` is the container health check for every role: for `web` and
`all` it fetches `/health` on `PORT`; for a worker or a scheduler it reads this host's own heartbeat row
and exits 1 when it is missing or stale. `salescoach status` prints the queue per owner (pending,
running, failed, the oldest pending event) under the recent calls.

**Deploying the three.** `render.yaml` describes one web service and two background workers from the
one Dockerfile plus a managed Postgres on the private network; each service sets `SALESCOACH_ROLE`
and gets `DATABASE_URL` from the database. `fly.toml` declares process groups `web`, `worker` and
`scheduler` (and `app`, the single-seller role `all` with its volume): `fly scale count app=0 web=1
worker=1 scheduler=1` for a team, `fly scale count worker=3` when the queue is long. Run `salescoach
migrate` (with `DATABASE_MIGRATE_URL`, the owner role) before the first deploy and after every upgrade:
`stores.sales()` refuses to serve a schema at the wrong version, on every role. Deploy the scheduler last
so the leader that takes over runs the new build.

Environment for the team install, beyond [deploy.md](deploy.md)'s table:

| Variable | Role | What it does |
|---|---|---|
| `SALESCOACH_MODE=cloud` | all three | multi-user semantics: no implicit user, org settings in the database, raw payloads in the database |
| `SALESCOACH_ROLE` | all three | `web`, `worker` or `scheduler` (`all` = one process) |
| `DATABASE_URL` | all three | the app role's `postgresql://` URL |
| `DATABASE_MIGRATE_URL` | `salescoach migrate` | the owner role's URL, for DDL |
| `WORKER_CONCURRENCY` | worker | loops per worker process (default 2) |
| `LLM_BUDGET_USER_USD_DAY`, `LLM_BUDGET_ORG_USD_DAY` | worker (and web, for the Usage panel) | daily model-spend caps, below |
| `SALESCOACH_PG_POOL_SIZE` | all three | connections per process (default 16); a worker needs concurrency + 2 |

## Model budgets

`LLM_BUDGET_USER_USD_DAY` and `LLM_BUDGET_ORG_USD_DAY` (or `llm.user_usd_day` / `llm.org_usd_day` in
the `budget` settings, which the environment overrides) cap what the models may cost per calendar day,
in USD. Before an agent calls a provider it sums `agent_runs.cost_usd` since the acting user's midnight
(their timezone) for that user and for everyone; past either cap it records a run with `error`
`budget_deferred: ...` and the worker defers the event without spending an attempt, exactly as for a
provider's 429, so the work resumes after the deferral (15 min) or the next day. Costs come from the
usage counts the API returns and the price table in `config/models.yaml` (`prices:` per model id, USD
per million input and output tokens; an unpriced model costs `None` and cannot be budgeted, so keep the
table current for the models you pick). The `claude_code` provider reports the CLI's own figure. Setup
> Model shows today's spend for the org and per user; `/me/setup` shows the user's own.

## Org settings and raw payloads

In cloud mode the settings overlay (what `data/settings/<name>.yaml` is on a laptop: the org profile,
the model choice, the sources, the methodology, the budgets) lives in the `org_settings` table, one row
per name with a `version` that every save bumps. `config.load()` reads the row's version on every call
and caches the merged settings by it, so web, worker and scheduler see a change on their next read with
no restart and no shared file. Secrets (API keys) stay in the environment or `secrets.env`; the style
guide is per user (`users.style`). Nothing in the wizard changes: it saves through the same function.

What a recorder, an upload, a paste or the webhook delivered is kept as it arrived: on a laptop under
`data/inbox/<kind>/`, in cloud mode in the `raw_payloads` table (owned by the user whose import it was,
one row per distinct payload per owner, written in the import's transaction so a refused import leaves
nothing). Nothing in the pipeline reads a payload back; `salescoach payloads export --out DIR --as
<user id>` (or without `--out`, JSON lines on stdout; `--since` to bound it) writes them out for support.
