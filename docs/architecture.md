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

**Migrations differ.** SQLite: `store/migrate.py`, keyed on `PRAGMA user_version`, run by every
connect, followed by the plugin DDL (`CREATE IF NOT EXISTS`) and `reconcile_columns`. Postgres:
`store/pgmigrate.py` applies the numbered files in `store/pg/` once, under `pg_advisory_lock`,
recorded in `schema_migrations`; `salescoach migrate` (`--check` in CI) is the only thing that runs
DDL, and `stores.sales()` refuses to serve a schema at the wrong version. `store/pg/0001_baseline.sql`
is generated from the SQLite files by `scripts/gen_pg_baseline.py` (`INTEGER PRIMARY KEY
AUTOINCREMENT` becomes an identity column, `REAL`/`BLOB` become `DOUBLE PRECISION`/`BYTEA`, PRAGMAs
are dropped, everything else verbatim); `tests/test_schema_parity.py` fails on any drift between the
committed file, the generator and a live Postgres database (tables, columns and their order, NOT
NULL, keys, indexes, defaults, CHECK counts). `store/tenancy.py` classifies every table OWNED / ORG /
SYSTEM for the multi-user work, and `tests/isolation/test_catalog_lint.py` fails on an unclassified one.

**SQLite-only, on purpose:** the file-based install, live capture and the local ASR, the Jarvis
bridge (`world.db`), the `user_version` migrations and table rebuilds (marked
`@pytest.mark.sqlite_only` in the suite).

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
