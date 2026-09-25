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

**Migrations differ.** SQLite: `store/migrate.py`, keyed on `PRAGMA user_version` (9 today; 8 is
Phase 3's and a missing number is skipped), run by
every connect, followed by the plugin DDL (`CREATE IF NOT EXISTS`) and `reconcile_columns`. Postgres:
`store/pgmigrate.py` applies the numbered files in `store/pg/` once, under `pg_advisory_lock`,
recorded in `schema_migrations` (0002 and 0005 today; 0003 and 0004 are Phases 2 and 3); `salescoach migrate` (`--check` in CI) is the only thing
that runs DDL, and `stores.sales()` refuses to serve a schema at the wrong version.
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
session settings `app.user_id` / `app.mode` (`PostgresConnection.bind_actor`; re-issued per
transaction and checked against RLS in Phase 2, hook `_on_begin`). `stores.sales()` binds whatever
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
which refuses a mismatch with an integrity error. Phase 1 scopes WRITES and the reads of the tables
that were re-keyed; the other reads still see every row until Phase 2's row-level security.

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
