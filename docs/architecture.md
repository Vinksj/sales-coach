# Architecture

One process, `salescoach serve`, holds everything: the FastAPI web app, the workflow worker, the
scheduler threads, the live-call manager and the live coach. State is one SQLite file plus audio
and settings files. Two small Swift programs sit outside it: `callcap` (capture) and
`coach-overlay` (the nudge panel).

```
 callcap (Swift) ──frames──> live/ ──> archive (FLAC), VAD, live ASR ──> hub (in memory) ──> live coach ──> overlay / live page
                                 │
 recorder export, upload,        │ CALL_ENDED
 folder, webhook, adapters ──> sources/base.import_normalized ──┐
                                                                ▼
                                       wf_events (durable bus) ──> worker ──> post-call pipeline
                                                                ▼
                 review UI (confirm loops, edit draft) ──> policy.approve_and_send ──> Gmail
                                                                ▼
                     scheduler: follow-ups, replies, calendar, sources, learning, (auto-send: off)
```

## Getting a call in

**Live.** `callcap` records the microphone (channel `me`) and system audio (channel `them`,
through the vendored AudioTeeCore) and streams frames to `salescoach/live/`. The live manager
allows one capture at a time. `start_call` creates the call at state `live` and publishes
`CALL_STARTED` only once `callcap` is really running; a capture that cannot start is left at
`capture_failed` with its error. Audio is archived crash-safely; after a crash, calls left at
`live` have their archive finalised on the next start. Segments pass a VAD and a live Whisper
pass. Levels, segments and status go to an **in-memory hub** with bounded queues that drop the
oldest message, so a stalled browser tab can never block capture. `stop_call` sets the state to
`captured` and publishes `CALL_ENDED` in the same commit.

**Everything else** goes through one function, `sources/base.py: import_normalized`, which
dedupes on `source_ref`, keeps the raw payload under `data/inbox/<kind>/`, resolves people by
email, decides which speaker is the seller (or holds the call in `needs_speaker` and asks, rather
than guess), stores the turns and publishes `CALL_ENDED`. See [sources.md](sources.md).

## The post-call pipeline

`orchestrator/workflow.py` is a per-call state machine:

```
captured -> final_transcribed -> diarized -> quality_done -> summarized -> analyzed
  -> actions_extracted -> loops_reconciled -> [strategized] -> email_drafted -> awaiting_review
then, from the review UI: reviewed -> email_sent | email_skipped -> done
```

- Each step runs, commits and advances `wf_state`, so a crash resumes at the next step. A failed
  step is retryable from the call page or with `salescoach process CALL --from STEP`.
- A call with no audio (a transcript import) skips transcription and diarization.
- A call in `needs_speaker` is never run, not even by an explicit re-run: channels decide whose
  commitment a quote is.
- `strategized` is not in the core. The intelligence plugin inserts it after `loops_reconciled`.
- An agent step is skipped when an artifact already exists for the same input hash. The hash
  covers the **rendered** system prompt and the input, so editing a prompt, the seller profile or
  the methodology invalidates exactly the work that depended on it.
- Heavy post-call work waits while a live capture is running, so a new call never competes with
  the previous call's analysis for memory.

### Agents

An agent has one responsibility, an explicit input and a pydantic output contract
(`agents/base.py`). Its system prompt is a versioned file under a `prompts/` folder, rendered
through `seller.render`. The output must validate; one retry with the validation error, then the
run fails and nothing downstream is written. Every attempt, failed ones included, is recorded in
`agent_runs` and committed at once, which is what the "Why?" views read. Agents only produce
structured output; the workflow validates it and decides what is stored.

Post-call agents: `quality`, `summary`, `call_analyst`, `actions`, `email`. Intelligence:
`deal_strategist`, `assessment_reconciler`, `prep_writer`, `longitudinal_coach`. Automation:
`followup`, `nudge`, `reply_analysis`. Live: `live_coach`. Which model runs each is in
[providers.md](providers.md).

## The event bus

`orchestrator/bus.py`, table `wf_events`.

- An event is written **before** it is handled, so a crash between the two leaves it pending and
  the worker picks it up on restart.
- `publish()` does not commit. The caller commits it together with the state change that caused
  it, so an event and its cause land atomically.
- `dedupe_key` is unique: `CALL_ENDED` for one call can be published any number of times and is
  stored once.
- The worker (`orchestrator/worker.py`) drains events one at a time in a background thread. A
  failed event waits `attempts x 30 s` and is tried at most 3 times; a rate limit defers without
  spending an attempt; an event whose handler crashed the process is parked, not re-claimed
  forever. A failed handler's partial writes are rolled back before the failure is recorded.
- Handlers may run again on retry, so they must be idempotent.
- Event types are plain strings; the core ones are listed in `schemas/events.py` and plugins add
  their own (`STRATEGY_REQUESTED`, `FOLLOW_UP_RUN`, `LEARNING_RECOMPUTE`, ...).

High-frequency live signals never touch this bus; they use the in-memory hub.

## Plugins

`salescoach/plugins/__init__.py`. A module in that package may define any of:

| Name | Purpose |
|---|---|
| `NAV` | extra top-navigation entries |
| `router` | a FastAPI router |
| `register(workflow)` | pipeline steps (`workflow.register_step(name, fn, after)`) and event handlers (`workflow.register_handler(type, fn)`) |
| `register_cli(subparsers)` | CLI subcommands |
| `start_background(db_path, stop)` | duties for `salescoach serve`; start threads, return quickly, honour the stop event |
| `<module>.sql` | schema beside the module, `CREATE ... IF NOT EXISTS` only, applied on every connect without importing Python |

Modules load in name order. One that fails to import, or whose `register` raises, is skipped and
recorded in `plugins.errors`; it never takes the core down. `SALESCOACH_NO_PLUGINS=1` runs the
bare core. A plugin puts its own tables behind the memory gate with `gate.register_table`.

| Plugin | Adds |
|---|---|
| `live_coach` | supervisor that attaches a coach to a live call; the nudge stream, timeline and replay; tables `nudges`, `coach_state` |
| `intelligence` | the `strategized` step, reconciliation, prep briefs, the selling report, the embedding index; tables `meddpicc`, `stakeholders`, `deal_risks`, `deal_health`, ... |
| `execution` | follow-up review, nudges, reply analysis, calendar, the scheduler threads, the auto-send executor; tables `followup_decisions`, `email_replies`, `calendar_meetings`, ... |
| `sources` | `/import/file`, `/import/webhook`, `/calls/<id>/speaker`, the polling duty |
| `learning` | recompute handlers, the Learning page, the deal outcome editor; tables `learned_patterns`, `pattern_observations`, `derived_outcomes`, ... |
| `setup` | only the Settings nav entry. The `/setup` routes are in the core, because the first-run gate redirects there and the page must exist with plugins disabled |

## Stores

- **`data/sales.db`** (SQLite, WAL, a 30 s busy timeout on every handle because the web server,
  the worker and the live pipeline all write it). Built on a small graph and event engine
  (`store/engine.py`): every node mutation goes through it and emits a row in `events`, so the
  audit log cannot drift from the tables. Core tables include `calls`, `turns`, `speakers`,
  `people`, `accounts`, `deals`, `deal_people`, `loops`, `claims`, `emails`, `email_edits`,
  `artifacts`, `agent_runs`, `field_provenance`, `memory_conflicts`, `wf_events` and `state`
  (small key-value facts such as the last provider test).
- **Migrations** (`store/migrate.py`) are keyed on `PRAGMA user_version`. A fresh database is
  created at the current version; append new steps, never edit a shipped one.
- **Files**: `data/calls/<id>/` audio, `data/inbox/` raw imports, `data/settings/` your settings
  and `secrets.env`.
- **Configuration**: `config.load(name)` deep-merges `config/<name>.yaml` with
  `data/settings/<name>.yaml`, cached on both files' modification times, so an edit is honoured
  on the next read.
- **Runtime folder** (`SALESCOACH_RUNTIME`): empty working directories for model CLI calls, kept
  away from any folder whose ancestors hold a `CLAUDE.md`.
- **Optional second store**: the bridge in `integrations/jarvis_bridge.py` mirrors confirmed
  loops into a private task system's database when that system is present on the machine
  (`WORLD_DB`, `JARVIS_DIR`). Absent, it does nothing.

## Two backends

The store runs on SQLite (the local install: one seller, one file) or on Postgres (`DATABASE_URL`;
what the multi-user, hosted version needs). One code base, one SQL dialect subset, both suites green
in CI (`SALESCOACH_TEST_DATABASE_URL` runs the same tests on Postgres, each test in a schema of its own).

**The layer.** `store/db.py` is the only place that knows there are two drivers. `stores.sales()`
returns its `Connection`: the sqlite3 surface the code was written against (`execute(sql, params)`
with `?`, rows by name and by index, `commit`/`rollback`, `in_transaction`, SQL strings for
`BEGIN`/`SAVEPOINT`) plus the few things the backends must agree on: `dialect`, `serialize(key)`
(the write lock / an advisory transaction lock), `lock_rows(sql, params, skip_locked=)`
(`FOR UPDATE [SKIP LOCKED]` / `BEGIN IMMEDIATE`), `insert_id(cursor)` (`RETURNING id` /
`lastrowid`), `table_exists(t)`, `columns(t)`, and the exception tuples `db.Error`,
`db.IntegrityError`, `db.OperationalError`. On Postgres every statement is translated once (cached
by its text) by a small tokenizer: `?` to `%s`, `:name` to `%(name)s`, `%` escaped, `INSERT OR
IGNORE` to `ON CONFLICT DO NOTHING`, `x IS y` / `x IS NOT y` to `IS [NOT] DISTINCT FROM`, `BEGIN
IMMEDIATE` to `BEGIN`, SQLite's NULL ordering made explicit (`NULLS FIRST` on ASC, `NULLS LAST` on
DESC, except bare columns that are NOT NULL everywhere), and `RETURNING id` added to an INSERT into a
table with an identity column. Transaction semantics are sqlite3's on both: a DML statement opens a
transaction, reads outside one run in autocommit, a SAVEPOINT outside one opens it. Postgres
connections come from a `psycopg_pool` pool; `close()` returns one.

**The dialect subset** (what new SQL must keep to; `tests/test_db.py` pins the translator):

- Types: timestamps are ISO-8601 TEXT (compare as strings, computed in Python; never `julianday`,
  `datetime('now')`, `strftime`); booleans are INTEGER 0/1; JSON is TEXT decoded in Python;
  ids are INTEGER (`RETURNING id` needs the column to be called `id`).
- Placeholders `?` (or `:name` with a dict); never string-format a value into SQL. A parameter
  compared with `IS NULL` needs a type: write `CAST(? AS TEXT) IS NOT NULL`.
- Upserts are `ON CONFLICT(cols) DO UPDATE SET c=excluded.c` and every other reference to the
  target table's columns in that SET is qualified (`deal_people.role_in_deal`). `INSERT OR IGNORE`
  is fine (translated); `INSERT OR REPLACE` / `REPLACE INTO` are refused.
- The new id of an INSERT is `db.insert_id(cursor)`, never `cursor.lastrowid`.
- Scalar `MAX(a, b)` is a `CASE`; `SUM(bool_expr)` is `SUM(CASE WHEN ... THEN 1 ELSE 0 END)`;
  `ORDER BY rowid` is `ORDER BY id`; an ORDER BY may name a SELECT alias only on its own, not inside
  an expression; every non-aggregated column of a GROUP BY query is in the GROUP BY (or the
  table's primary key is).
- Row locks: `conn.lock_rows(...)` and `conn.serialize(key)`, not `BEGIN IMMEDIATE`.
- `LIKE` is case-sensitive on Postgres and case-insensitive (ASCII) on SQLite: use it on ids and
  fixed prefixes, and `db.like(col, ci=True)` when case must not matter (the 17 existing uses are
  all on ids, JSON fragments or already lower-cased text).
- Schema questions go through `conn.table_exists()` / `conn.columns()`, not `sqlite_master` or
  `PRAGMA`. Plugin DDL, `reconcile_columns` and the runtime `ensure_columns()` helpers are
  SQLite-only paths; a new column goes into the tracked SQL and a numbered Postgres migration.
- After a database error inside a transaction, roll back before doing anything else: a failed
  statement aborts a Postgres transaction. Prefer `ON CONFLICT` to catching `IntegrityError`.
- Catch `db.IntegrityError` / `db.OperationalError` / `db.Error`, never `sqlite3.*`.

**Migrations differ.** SQLite: `store/migrate.py`, keyed on `PRAGMA user_version` (9 today: 8 is
Phase 3's sign-in tables, 9 is Phase 6's ops tables), run by
every connect, followed by the plugin DDL (`CREATE IF NOT EXISTS`) and `reconcile_columns`. Postgres:
`store/pgmigrate.py` applies the numbered files in `store/pg/` once, under `pg_advisory_lock`,
recorded in `schema_migrations` (0001, 0002, 0004 and 0005 today: 0003 was the one-shot row-level
security file, replaced by the repeatable `rls.sql` before anything was deployed, and the gap is
kept so a database that did apply it moves on through 0004; the highest number is the version a build
expects), then the REPEATABLE `store/pg/rls.sql` whenever its sha256 differs from the one recorded in
`schema_repeatables` (in the same lock, in one transaction); `salescoach migrate` (`--check` in CI) is
the only thing that runs DDL, and `stores.sales()` refuses to serve a schema at the wrong version or
whose applied `rls.sql` is not this build's.
`store/pg/0001_baseline.sql` was generated from the version-6 SQLite files by
`scripts/gen_pg_baseline.py` (`INTEGER PRIMARY KEY AUTOINCREMENT` becomes an identity column,
`REAL`/`BLOB` become `DOUBLE PRECISION`/`BYTEA`, PRAGMAs are dropped, everything else verbatim) and is
**frozen**: every later change is a hand-written numbered file beside it (`0002_owner.sql` is SQLite
migration 7), because a database that applied 0001 can only move on through them. A schema change
therefore lands in three places: the tracked SQLite files (the source of truth, what a fresh SQLite
store gets), a `store/migrate.py` step, and a `store/pg/NNNN_*.sql` file; `tests/test_schema_parity.py`
fails on any drift between the tracked files and a live Postgres database migrated through every
numbered file (tables, columns and their order, NOT NULL, keys, indexes, defaults, CHECK counts; the
one default that differs by design is `owner_id`, below). `store/tenancy.py` classifies every table
OWNED / ORG / SYSTEM, and `tests/isolation/test_catalog_lint.py` fails on an unclassified one, on an
OWNED table without `owner_id` and an index on it, and (live) on a Postgres OWNED table whose default
is not the session setting or a child table without its trigger.

**SQLite-only, on purpose:** the file-based install (one user, `local`), live capture and the local ASR, the Jarvis
bridge (`world.db`), the `user_version` migrations and table rebuilds (marked
`@pytest.mark.sqlite_only` in the suite).

## Identity and ownership (Phase 1 of the multi-user work)

**Who is acting.** `salescoach/identity.py` holds the acting user: an `Actor(user_id, mode, role,
profile)` in a contextvar, bound to the store connection as `conn.actor` and, on Postgres, to the
session settings `app.user_id` / `app.mode` (`PostgresConnection.bind_actor` for the session,
`_on_begin` transaction-locally at the start of every transaction; the row-level policies read
them, see "Isolation" below). `stores.sales()` binds whatever
actor is current when it opens. Two modes, `SALESCOACH_MODE`:

- `local` (default): the one user, `"local"`, is implicit everywhere, so a CLI command, a test, a
  bare thread all act as the local user and the single-user product is unchanged. A SQLite store is
  always local: `users.create` refuses a second user on SQLite, and every `owner_id` there is
  `'local'` by column default whoever acts.
- `cloud`: Postgres only (`stores.sales()` refuses a SQLite path; `serve` refuses at the door). There
  is no implicit user: `identity.current_actor()` and `seller.profile()` outside a session raise
  `NoActor`, which is how a background thread that forgot its session is caught. Live capture, the
  recorder duty, the live-coach plugin, the Claude CLI calendar connector and the Jarvis bridge are
  off (`hosted.is_hosted()` is also true, so Setup says "not available in a hosted install").

The only ways to set the actor: `identity.session(user_id, mode=)` (opens a connection and binds
everything; every thread entry point uses it or `as_user`), `as_user(conn, user_id)` (re-bind an
existing connection), `activate(actor)` (the contextvar alone, for a thread that opens its own
store later), and the web layer's `ActorGate` (local: the local user; cloud: the login cookie's user
looked up in `users`, a stub Phase 3 replaces with Google sign-in). Thread entry points:

| Thread | Actor |
|---|---|
| worker (`orchestrator/worker.py`) | the connection is opened as nobody (`activate(None)`); `workflow.handle` wraps each event in `as_user(owner_of_event(...), mode="service")`: entity `user:<id>` says the owner, a call/deal/loop id resolves through `nodes.owner_id`, else `payload.owner_id`, else `wf_events.owner` (what `bus.publish` resolved from the publisher's actor), else the local user (local mode) or `UnknownOwner` (cloud). `bus.claim_next` hands out the highest-priority, oldest event whose owner has nothing running (a per-owner advisory lock on Postgres); a `worker` process runs `WORKER_CONCURRENCY` such loops (`ops.py`, docs/deploy-cloud.md) |
| scheduler (`automation/scheduler._loop`) | a per-user duty runs once per `users.active()` inside `as_user(..., service)`; bookkeeping goes to `user_state`. An org-level duty (`per_user=False`: the sources poller until Phase 4) runs once as the local user and not at all in cloud. In the split deploy the duties (and the embed loop below) run only in the `scheduler` role's leader process (`ops.run_scheduler`, a Postgres advisory lock) |
| heartbeats (`ops.Heartbeat`) | nobody (`activate(None)`): `state` is SYSTEM. `ops:worker:<host>:heartbeat`, `ops:scheduler:<host>:heartbeat`, `ops:scheduler:leader` |
| intel embed (`plugins/intelligence.py`) | per active user, `as_user(..., service)` |
| learning daily (`plugins/learning.py`) | a per-user scheduler duty |
| jarvis sync (`web/app.py`) | `session("local", service)`; not started in cloud |
| live-coach replay (`plugins/live_coach.py`) | the requesting actor is captured before the thread starts and re-entered with `activate()` inside it |

**The profile is two halves** (`seller.py`): `ORG_FIELDS` (company, website, offering, icp,
buyer_titles, vocabulary, own_domains) from `seller.yaml`, one per install; `USER_FIELDS` (name,
emails, role title, aliases, languages, timezone, signature, call_context, plus the style guide) from
`seller.yaml`/`style.md` for the local user and from the acting user's `users` row for anyone else.
`profile()` merges them; `org_configured()` and `user_configured()` split the old `is_configured()`,
and the `FirstRunGate` sends an unconfigured org to `/setup` and an unconfigured user to `/me/setup`
(the local user's is `/setup/you`). `users.py` holds `users`, `teams`, `team_managers`, `user_state`
and `user_speaker_labels`; the local install has one `users` row, `local`, created from `seller.yaml`
on first open and kept in step by `/setup/you`.

**people.user_id** is the person row that IS a user (`repo.ensure_me` / `sync_me` key on it for the
acting user); `is_me` is derived (1 iff `user_id IS NOT NULL`) and kept so untouched readers work,
now meaning *any internal user*. Every `is_me` read was classified:

| Meaning | Sites | Now |
|---|---|---|
| the acting owner (ME) | `repo.ensure_me/sync_me/me_row`, `learning.seller_id`, `coach/slow_pass` (my names), `sources/base.me_addresses` (+ granola), `intel/history` (the `me` stakeholder), the ` (ME)` label in `orchestrator/context.me_label` and `intel/history`, `deal.html` / `live.html` "(you)", `intel/strategist` (the reject reason) | `people.user_id = actor` (`me_label` says ` (colleague)` for another user's row) |
| any internal user (never a buyer) | every `is_me=0` predicate: `intel/tables`, `intel/history` (buyers/others/extra), `intel/strategist` (name matches), `web/app.py` people list and buyers, `learning/outcomes`, `learning/observe`, `coach/slow_pass` (stakeholders), `integrations/jarvis_bridge`, `automation/calendar`, `automation/followup`, `validators/recipients`, `orchestrator/context` (buyer names, stakeholders), `agents/email_drafter`, `sources/base._people_by_label`, `repo.call_participants` ordering, `automation/common.my_addresses` | unchanged (`is_me`) |

**owner_id.** Every OWNED table (`store/tenancy.py`) has `owner_id TEXT NOT NULL` with an index
(`idx_<table>_owner_id`); `nodes.owner_id` is NULL for account and person nodes (the org directory,
`OWNER_NULLABLE`, a CHECK says so) and NOT NULL for call/deal/loop nodes. The default is `'local'`
on SQLite and `NULLIF(current_setting('app.user_id', true), '')` on Postgres, so an owned INSERT with
no acting user fails its NOT NULL (fails closed). Child rows (`tenancy.OWNER_PARENTS`: turns, speakers,
participants, artifacts, claims, assessments, agent_runs, observations, loops, emails and their edits
/ slot fills / autosend log, replies and proposals, follow-up decisions, the deal tables, embeddings,
pattern observations, nudges, coach state, and calls/deals/loops/edges/events/sources under their
node) take the parent's owner through the `app_child_owner()` BEFORE INSERT trigger on Postgres,
which refuses a mismatch with an integrity error. Writes and the re-keyed tables' reads are scoped
in the code; every other read is scoped by the database ("Isolation", below).

| Class | Tables |
|---|---|
| OWNED (owner_id) | nodes (nullable), edges, events, sources, deals, deal_people, calls, call_participants, turns, speakers, agent_runs, artifacts, claims, assessments, reconciliations, loops, emails, email_edits, seller_observations, seller_patterns, field_provenance, memory_conflicts, followup_decisions, email_replies, reply_proposals, calendar_cache, calendar_meetings, slot_fills, autosend_log, stakeholders, meddpicc, deal_risks, deal_health, deal_health_history, coach_reports, prep_briefs, embeddings, deal_stage_history, derived_outcomes, pattern_observations, learned_patterns, learning_proposals, nudges, coach_state |
| ORG | accounts, people, users, teams, team_managers |
| SYSTEM | wf_events, state, user_state, user_speaker_labels, schema_migrations, org_settings |

Phase 6 added `raw_payloads` (OWNED, top-level: the acting user's import, no parent row, no trigger)
and `org_settings` (SYSTEM: the settings overlay in cloud mode, `config.py`). `wf_events.owner` is a
routing key for the bus's fairness rule, not a rep's data, which is why it is `owner`, not `owner_id`,
and the table stays SYSTEM.

**Keys re-scoped per owner** (migration 7 / 0002): `seller_patterns (owner_id, tag)`,
`calendar_cache (owner_id, key)`, `calendar_meetings (owner_id, event_id)`, `email_replies UNIQUE
(owner_id, message_id)`, `learned_patterns UNIQUE (owner_id, family, key, scope)` with ids
`lp:<family>:u:<owner>:<key>` (rewritten in `merged_into`, `learning_proposals`, `field_provenance`
and `memory_conflicts`), `coach_reports.owner_id`. A user's own paste or upload gets
`paste:<owner>:<sha>` / `upload:<owner>:<sha>` (`repo.user_source_ref`); recorder-native and
folder/webhook refs are unchanged until Phase 4. Ownerless events name their user: `FOLLOW_UP_RUN`
and `CALENDAR_REFRESH_REQUESTED` carry `entity_id = user:<owner>`, `COACH_REPORT_REQUESTED` too, and
every timestamp-based dedupe key carries the owner. `sources.yaml me_labels` became
`user_speaker_labels` (adopted for the local user on first open).

**state vs user_state.** `stores.get_state/set_state` is org-wide; `get_user_state/set_user_state`
is the acting user's (`user_id` from `conn.actor`):

| Org-wide (`state`) | Per user (`user_state`) |
|---|---|
| `sources:<kind>:last_run/last_ok/last_result/last_error` and the adapters' cursors (the poller is org-level until Phase 4); `automation:sources:*` | `automation:followups:ran_for`, `automation:followups:last_eval`, `automation:replies:last_poll`, `automation:calendar:last_sync`, `automation:<duty>:last_run/last_result/unavailable/last_error` for every per-user duty (followups, replies, calendar, autosend, recorder, learning) |
| `setup:provider_test`, `setup:finished_at`, `setup:key_host:<provider>` (the org's model provider and wizard); `ops:worker:<host>:heartbeat`, `ops:scheduler:<host>:heartbeat`, `ops:scheduler:leader` (process liveness, `ops.py`) | `setup:card_dismissed` (the Today card) |
| `automation:calendar_tools` (the connector discovery on this machine) | `intel:coach_error`, `learning:last_run`, `learning:last_error` |

## Isolation

**The database enforces visibility.** On Postgres every table has row-level security and the app
connects as a role the policies apply to, so a query without an owner clause returns the acting
user's rows and nothing else (`tests/isolation/test_deliberate_leak.py` removes owner clauses on
purpose and proves it). SQLite has no policies and needs none: one file is one user.

**The settings contract.** Two session settings, and only two: `app.user_id` (the acting user's id;
`''` for nobody) and `app.mode` (`interactive` = a person at the keyboard, `service` = a background
duty). `PostgresConnection.bind_actor` sets both for the session from `conn.actor` (and
transaction-locally too when a transaction is open, so `as_user` mid-transaction is seen at once);
`_on_begin` re-issues them transaction-locally at the start of **every** transaction (an explicit
`BEGIN`, a `SAVEPOINT` outside one, the implicit one a DML statement opens, `serialize()`,
`lock_rows()`), because the code commits mid-function and a `SET LOCAL` issued once would die with
the first `COMMIT`. A pooled connection is reset to `''` when it is returned. Nothing else may `SET`
these. An unset `app.user_id` sees no OWNED row and cannot write one.

**The debug assertion.** With `store/db.ASSERT_ACTOR` on (the whole test suite;
`SALESCOACH_ASSERT_ACTOR=1` elsewhere), a statement on Postgres with `conn.actor is None` raises
`db.NoActorBound` unless the connection is inside `conn.as_system()` (or has `conn.system = True`).
In production such a statement is safe but silent (it sees nothing); the assertion is what turns a
forgotten `identity.session()` in a background thread, or a route the gate let through as nobody,
into a failing test. The system scopes, all deliberate: the migrator (`pgmigrate.apply`), the
store's own version check and the local-user bootstrap (`stores._postgres` / `sales()`),
`identity._load` (reading the users row that becomes the actor), the `ActorGate`'s lookup,
`/health`, the scheduler's and the embed loop's `users.active()` listing, and the worker's
connection (the claim loop runs as nobody; `workflow.handle` binds the event's owner, learnt through
`repo.owner_of` → `app_owner_of()`, a SECURITY DEFINER function that returns an owner id and nothing
else).

**The two roles.** `DATABASE_URL` names the **app role**, `salescoach_app`: `LOGIN`, no superuser,
no `BYPASSRLS`, no `CREATE`; it has `SELECT/INSERT/UPDATE/DELETE` on the tables, `USAGE` on the
sequences and only `SELECT` on `schema_migrations`. `DATABASE_MIGRATE_URL` names the **owner role**:
it owns every table and the helper functions, runs `salescoach migrate` and is never used at
runtime; it must have `BYPASSRLS` (or be a superuser) so that a migration sees every row and the
SECURITY DEFINER helpers can read the directory and `nodes`. `pgmigrate.apply` runs with
`row_security = off`, so an owner role without the bypass fails a migration loudly instead of
silently touching no rows. The test suite creates `salescoach_app` in the container at session start
and hands the app that URL (`tests/conftest.py`); a deployment creates it once:
`CREATE ROLE salescoach_app LOGIN PASSWORD '…' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE … TO salescoach_app;` then `salescoach migrate` grants the rest
(`store/pg/rls.sql` refuses to apply if the role is missing or bypasses RLS).

**The policies**, generated from `store/tenancy.py` by `store/rls.py` (`scripts/gen_pg_rls.py
--write`; `tests/isolation/test_rls_generated.py` fails when the committed `rls.sql` differs). The file
is a repeatable step and describes the WHOLE policy set, idempotently: a leading `DO` block drops every
policy on every table of the schema (so a removed or renamed policy never lingers), functions are
`CREATE OR REPLACE`, triggers are dropped before they are created, every table states `FORCE` or `NO
FORCE`, and the app role's grants are re-issued. That is what lets a table created by a later numbered
migration (Phase 3's `sessions`, `invites`, `oauth_tokens`; Phase 6's `org_settings`, `raw_payloads`)
be policed: a numbered policy file could only ever cover the tables that existed when it ran.
The helpers: `app_actor_id()` (the setting, NULL when empty), `app_mode_ok()` (a mode is bound),
`app_actor_active()` / `app_actor_role()` (the acting user's row exists and is `active`, SECURITY
DEFINER so the `users` policies cannot recurse), `app_visible_owners()` (the acting user's id plus
the ids of every member of every team they manage, `team_managers` + `users.team_id`, read live so a
demotion or a disabling takes effect on the next query; empty for nobody, for an inactive user and
for an admin who manages no team), `app_can_write(owner)` (the row is the acting user's own, they are
active, a mode is bound), `app_users_empty()`, `app_owner_of(node)`, `app_org_spend_since(since)` and
`app_spend_by_owner_since(since)` (Phase 6's budgets: the org cap must count every rep's runs, which no
rep may read; sums per owner, never a row; per owner only for an admin or the owners one may read), and
`app_child_owner()` (the 0002 trigger, made SECURITY DEFINER so it sees another user's parent row and
refuses the mismatch).

| Class | RLS | SELECT | INSERT / UPDATE / DELETE |
|---|---|---|---|
| OWNED | enabled + **forced** | `owner_id = ANY(app_visible_owners())`; `nodes` also lets any active user read a NULL owner (the directory's account and person nodes) | `app_can_write(owner_id)`: the owner, active, in a bound mode. A manager reads and never writes a rep's row; `UPDATE` cannot move a row to another owner (`WITH CHECK`) |
| ORG: `accounts`, `people` | enabled | any active user | any active user in a bound mode (every rep contributes to the shared directory) |
| ORG: `users`, `teams`, `team_managers` | enabled | every connection (a connection reads `users` to learn who it is) | admins; a user may `UPDATE` their own `users` row, and `trg_users_guard` refuses a non-admin (on the app role) changing `id`, `role`, `team_id` or `status`, except accepting their own invite (`invited` → `active`) at their first sign-in; the first row of an empty `users` may be inserted by anyone (the local user, the bootstrap admin) |
| SYSTEM: `wf_events` | enabled | every connection | every connection: the bus carries ids and step names, never content; interactive requests publish, the worker claims as nobody. Whose event it is, is decided in code (`event_retry` 404s another user's) |
| SYSTEM: `state` | enabled | any active user; the `ops:%` heartbeat rows (host, pid, timestamps, counts) by every connection | admins and service-mode duties; the `ops:%` rows also by nobody (worker and scheduler processes act for nobody, `/health` reads them before sign-in); `DELETE` admins and duties only |
| SYSTEM: `user_state`, `user_speaker_labels` | enabled | own rows (`user_id = app_actor_id()`) | own rows |
| SYSTEM: `schema_migrations`, `schema_repeatables` | enabled | every connection | nobody (no privilege) |
| SYSTEM: `sessions` | enabled | every connection | every connection. The `AuthGate` resolves a session before anyone is bound, so no owner rule can apply; `sessions.id` is **sha256 of the session id** (`sessions.key`), the cookie keeps the raw id plus its HMAC, so the row is found only by someone holding the cookie's secret and a copy of the table replays nothing (the wf_events reasoning: machinery keyed by an unguessable value) |
| SYSTEM: `invites` | enabled | every connection (the Google callback's allow-list, nobody bound) | `UPDATE` every connection (the callback marks it accepted); `INSERT` / `DELETE` admins |
| SYSTEM: `oauth_tokens` | enabled | the acting user's own grant (`user_id = app_actor_id()`, active), or any grant for an active admin (disabling revokes grants; the admin page shows link status); nobody reads none | the same. `salescoach tokens rotate` runs as the owner role (`DATABASE_MIGRATE_URL`) or `--as` an active admin, explicitly |
| SYSTEM: `org_settings` | enabled | every connection: `config.load()` reads the overlay from every thread, including before anyone is bound (the scheduler's intervals, the sign-in page's brand); it holds no per-user data and no secrets | active admins (Settings, which is admin-only in cloud mode: `setupui` answers 403 to anyone else) |

Managers get **SELECT only**; "a manager can never send" is also a hard check in the code:
`execution/policy.approve_and_send`, `mark_sent_manually` and `acknowledge_not_sent` raise
`SendRefused` unless `conn.actor` is the email's owner acting interactively (the local install's
auto-send executor, `approved_by='policy:…'`, is the owner's own service duty and is refused in cloud
mode). The web layer's `ActorGate` answers a request with no valid session before any store is
opened (a page is sent to `/login`, anything else gets 401), a session for a user who is not
`active` counts for nothing (it binds nobody and is answered like no session: Phase 3's `AuthGate`), and
open paths (`/login`, `/logout`, `/health`, `/static`, the webhook, the Google callback) pass with nobody
bound and read the store as system.

**Adding a table.** Classify it in `tenancy.TABLE_CLASS` (the catalog lint fails until you do). OWNED:
give it `owner_id TEXT NOT NULL DEFAULT 'local'` with an index in the SQLite files, the Postgres
default `NULLIF(current_setting('app.user_id', true), '')` and, when its owner is a parent row's,
an entry in `tenancy.OWNER_PARENTS` with the `app_child_owner` trigger; then run
`scripts/gen_pg_rls.py --write` and commit `rls.sql`: the next `salescoach migrate` applies the new
numbered step and then re-applies the whole policy set, the new table included (no hand-written
policy block, no edit to an earlier step). `tests/isolation/factories.py` derives the matrix row
from the schema; if a column needs a value the rules cannot derive, add it to `factories.VALUES`, or
the matrix fails with `NoFactory`. ORG / SYSTEM tables need a policy set in `rls.ORG_POLICIES` /
`rls.SYSTEM_POLICIES`. Grants for new tables come from `ALTER DEFAULT PRIVILEGES` and the grants
`rls.sql` re-issues.

**Adding a route.** A route with a path parameter must be placed in
`tests/isolation/test_route_crawl.py`: either its parameter names resolve to rep A's objects (add an
object of the new kind to the `objects` fixture) so the crawl requests it as rep B and demands a 404,
or the route is listed in `NOT_A_USERS_OBJECT` with the reason it is not one user's object. The
inventory test fails until one of the two is done. Look objects up through the store as the acting
user and let a missing row be a 404; never a 403 (it confirms the object exists) and never an empty
200.

**Caches.** Every `functools.lru_cache` in `salescoach/` is allow-listed in
`tests/isolation/test_caches.py` with the parameters its key is made of; none is keyed on the acting
user or reads the store. `intel/methodology._library` is keyed on the three files' paths and stamps
plus `methodology.settings_version()`, the hook for the day methodology settings live in the
database and a file stamp would go stale across processes.

**Sign-in, sessions and grants (Phase 3).** In cloud mode the acting user comes from Google
sign-in (`web/auth.py`, `googleauth.py`): `/auth/google` keeps `state`, `nonce` and a PKCE verifier
server-side; `/auth/callback` exchanges the code, parses the ID token with Authlib against Google's
JWKS and verifies it again with google-auth, then checks `email_verified`, `hd` in
`GOOGLE_ALLOWED_DOMAINS` and the invite list (`users.status = invited|active`, or
`SALESCOACH_BOOTSTRAP_ADMIN` once). The session is a row in `sessions` (`salescoach/sessions.py`)
keyed on sha256 of the session id; the cookie is only the id and its HMAC, and the `AuthGate` reads the session and the `users` row on every
request (sliding thirty days; revoke, revoke all, disable are immediate), leaving the `Actor` in
`scope["state"]` for the `ActorGate`. Password mode (`hosted.py`) is unchanged for a single-seller
hosted install. Per-user OAuth grants live in `oauth_tokens` (`execution/tokens.py`: AES-256-GCM
under the `SALESCOACH_TOKEN_KEYS` ring, one live row per user and provider, scopes `gmail.compose`
+ `gmail.readonly` and `calendar.readonly`, `invalid_grant` marks `needs_reconsent` once);
`GmailProvider.for_user` builds a mailbox from one, `provider_for(conn)` picks it in cloud and the
machine-local alias otherwise, and `policy.approve_and_send` refuses in cloud anyone but the
email's owner in an interactive session (auto-send is off). Sends carry `X-Salescoach-Key`; an
unknown-outcome send is recovered from Sent by that header, not by a Message-ID Gmail may have
replaced. Admin writes (`adminui/ops.py`) land in `events` with `actor_user_id`. The tables
`sessions`, `invites` and `oauth_tokens` are SYSTEM in `store/tenancy.py`. See
[deploy-cloud.md](deploy-cloud.md).

**Managers (Phase 7).** A manager reads their team's rows through the same policies and writes none;
the app adds the read-only rule on top (`manager/access.py`). Every non-GET request runs inside
`access.write_request()` (the `ActorGate`), and every `*_or_404` helper passes its row through
`access.guard()`, which raises `ReadOnly` (403) when the acting user can read the row but does not own
it: the refusal comes before anything is written or queued, instead of a policy's silent no-op. The bus
(`orchestrator/bus.publish`) refuses an interactive publish on someone else's object, since `wf_events`
is the one table every connection writes and the worker handles an event as its owner. Pages decide
read-only rendering from the object's `owner_id` (`access.page_owner`); the Coach and Learning pages
of a rep are rendered inside `identity.viewing(rep)`, which changes whose rows the own-work reads
select (`identity.subject_id`) and nothing else. `comments` is the one OWNED table a non-owner inserts
into (`store/rls.OWNED_EXCEPTIONS`, with its reason); `access_log` is SYSTEM and insert-only. Comments,
coaching notes and the team roll-up are never read by a prompt-building module
(`tests/test_manager.py`). See [manager.md](manager.md).

## Prompts

Every prompt is a Markdown file beside its agent, with `{{variables}}` filled by
`seller.render` from the seller profile (and, for the strategist, the methodology:
`{{ELEMENTS}}`, `{{HEALTH_RUBRIC}}`, ...). Only `{{name}}` is substituted: prompts contain literal
single-brace markers (`{garbled}`, `{bleed}`) that must reach the model untouched, so
`str.format` is never used. An unknown variable raises, so a typo fails loudly instead of
reaching a model. A variable that is empty for this seller takes its leading space with it.
No tracked prompt or config file names a person or a company.

## The safety checks

| Check | Where | What it guarantees |
|---|---|---|
| Evidence | `validators/evidence.py` | A quote's words must appear, in order, in the cited turns (neighbours only as a fallback). A dropped or added negation means not supported. A loose match caps at medium confidence; only an exact match may close or change a loop. The owner check uses the channel of the turns the quote actually matched. Garbled or bleed-flagged turns cap confidence at medium |
| Confidence gate | `validators/gates.py` | Nothing feeds an email or another external action below `policy.yaml external_actions.min_confidence` (medium), and the system's own recommendations never do, unless you confirmed the item |
| Memory gate | `memory/gate.py` | Every stored fact has confidence and provenance. User input always wins and closes open conflicts on the field; a value backed by explicit evidence or user input is never overwritten by a weaker claim, and the disagreement is kept in `memory_conflicts`. Only allow-listed tables and columns can be written, which keeps a model's field name out of SQL |
| Review | `orchestrator/review.py` | Confirming a loop is user input and is the only way an inferred item becomes something the system acts on with full confidence |
| Recipients | `validators/recipients.py` | An email may only address the call's participants and the deal's people, minus the seller, plus addresses the seller typed |
| Voice lint | `validators/voice_lint.py` | Banned words and "when works for you" phrasing are flagged; blocking issues stop approval. Any bracketed placeholder blocks Send |
| Email policy | `execution/policy.py` | The one path to Gmail. `approve_and_send` takes an immediate transaction, refuses anything already sent, marks the row `sending`, and uses a deterministic Message-ID. An error before Gmail could have accepted the message marks it failed (retryable); any other error leaves it "delivery unknown", and a retry first searches Sent |
| Gmail | `execution/gmail.py` | No LLM in the module; it sends exactly the bytes it is handed. The token file is read-only here |
| Auto-send | `automation/autosend.py` | Off unless `policy.yaml` names an auto-send policy **and** `automation.yaml` has `enabled: true` and `dry_run: false`; then still subject to lint, recipient, content, hold-time, cap and working-hours checks, and still through `approve_and_send` |
| Calendar | `automation/connector.py` | Read-only three ways: the module refuses to build a call to a tool outside its read list, only that one tool is allowed, and every write tool is explicitly disallowed. Tool results are read verbatim; no model retypes calendar data |
| Web | `web/app.py` | Host allow-list, exact-origin check on every state-changing request, first-run gate. See [SECURITY.md](../SECURITY.md) |
| Secrets | `config.py`, `providers/` | 0600 file, never logged, never echoed, scrubbed from error text |
| Learning | `learning/` | No model calls; counts only; nothing from a call or an email reaches a prompt. See [learning.md](learning.md) |

Two reviews of these paths, what they found and the regression tests that pin each fix are in
[review-2026-09-12.md](review-2026-09-12.md).
