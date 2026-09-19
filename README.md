# Sales Coach

Sales Coach is a local-first assistant for one B2B seller. It records or imports sales calls,
transcribes audio on the machine, analyses each call with LLM agents, turns commitments into
tracked open loops, scores each deal against your sales methodology, and drafts the follow-up
email. It is for account executives, founders who sell, and sales engineers who want this on
their own computer, with their own model provider, instead of in a hosted service. Nothing is
emailed unless you press Send.

It started as one person's tool and has been generalised: a setup wizard captures who you are,
how you sell, which model you use and where your calls come from. Read
[Status and known gaps](#status-and-known-gaps) before you rely on it.

## What it does

- **Gets calls in.** Live capture on macOS (microphone = you, system audio = the other side),
  an uploaded recording, a pasted or uploaded transcript (`.txt`, `.vtt`, `.srt`, JSON), a watched
  folder, a webhook, and polling adapters for Fireflies, Fathom and Granola. One import path for
  all of them; the same transcript never becomes two calls. See [docs/sources.md](docs/sources.md).
- **Transcribes locally.** Whisper models through MLX on Apple Silicon, optional speaker
  separation with sherpa-onnx. Models are downloaded only when you ask (`salescoach models pull`).
- **Analyses each call** in a fixed pipeline: transcript quality, summary, call analysis, action
  extraction, reconciliation with existing open loops, follow-up draft. Every agent returns
  structured output that code validates before anything is stored.
- **Checks evidence.** A commitment or a claim must quote words that are found in the turns it
  cites. A quote that drops or adds a negation is rejected. Unsupported items stay low confidence
  and cannot feed an email.
- **Tracks open loops** (who owes what, by when), with a follow-up cadence, a daily follow-up
  review that decides nudge / wait / escalate / close as stale / ask you, and reply polling that
  maps a buyer's reply to the loops it answers.
- **Scores deals against your methodology.** MEDDPICC, MEDDIC, BANT, SPICED, SPIN, Challenger,
  Sandler, Command of the Message, or one you define. An element counts as known only with a
  verified buyer quote. Deal health is capped in code by facts (one buyer-side voice, a critical
  element unknown, no dated buyer commitment). See [docs/methodologies.md](docs/methodologies.md).
- **Prepares you.** A pre-call brief per deal (open loops, gaps, questions), and a report on how
  you sell across calls, with evidence.
- **Coaches live.** During a captured call, fast rules plus a model pass about once a minute put
  at most one short nudge on screen at a time, with a cooldown and a per-call budget. An optional
  always-on-top overlay shows it over your meeting app. Every held-back nudge is logged with the
  reason, and a finished call can be replayed to tune thresholds.
- **Drafts emails, never sends them on its own.** Drafts follow your style guide, are linted,
  may only address people on the call or the deal, and block on any unfilled placeholder. `[SLOTS]`
  is filled with calendar-verified times.
- **Learns patterns over time**, counted in code from stored facts: how you sell, how you edit
  drafts, which follow-ups get replies, which live nudges you act on. You can confirm, reject or
  retire anything it believes. See [docs/learning.md](docs/learning.md).
- **Your choice of model provider:** the Claude CLI on a Claude subscription, the Anthropic API,
  OpenAI, xAI, any OpenAI-compatible endpoint, or Ollama. See [docs/providers.md](docs/providers.md).

## Quickstart

The full guide, including live capture permissions, keeping it running, upgrading and
uninstalling, is [docs/install.md](docs/install.md). The short version:

Requirements:

- Python 3.11, 3.12 or 3.13.
- macOS 14.4 or later on Apple Silicon for live capture and local transcription. Everything else
  runs anywhere Python runs (see [Platforms](#platforms)).
- Optional: `ffmpeg` to import audio recordings; the Swift toolchain (Xcode command line tools)
  to build the capture helper and the overlay; the `claude` CLI if you want to use a Claude
  subscription as the model, the calendar, or Granola.

```bash
git clone <repository-url> salescoach        # the repository URL is not published yet
cd salescoach
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"            # the app, plus pytest and ruff
.venv/bin/pip install -e ".[asr]"            # only for local transcription (Apple Silicon)
.venv/bin/salescoach serve
```

Open <http://127.0.0.1:8140>. Until your profile exists, every page leads to the setup wizard,
which walks you through the rest: your profile, your methodology, the model provider and its key,
where calls come from, and a read-only status of email, calendar, capture and speech models.
[docs/setup.md](docs/setup.md) describes each step and what it changes.

Install with `pip install -e .` from a clone. That is the supported method: the tracked defaults
in `config/`, the Swift helpers and the launchd scripts live beside the Python package and are
found relative to the checkout. A plain `pip install .` does not ship them.

For live capture on macOS, also:

```bash
callcap/build.sh          # builds and signs callcap/build/callcap
overlay/build.sh          # optional: the always-on-top nudge panel
```

then allow the recorder once in System Settings > Privacy & Security: **Microphone**, and
**Screen & System Audio Recording** ("System Audio Recording Only"). Without the grant it records
silence, and the app cannot check the grant for you. `callcap/build.sh` explains how to sign with
your own certificate so the grant survives a rebuild.

To keep the server running from login on macOS: `sh launchd/install.sh` (remove it with
`sh launchd/uninstall.sh`). The plist is generated for your checkout, your user and the tools
found on your machine; the log is `~/Library/Logs/salescoach/serve.log`.

## Platforms

| Feature | macOS 14.4+ (Apple Silicon) | Other macOS, Linux, Windows |
|---|---|---|
| Web app, pipeline, deals, loops, prep, learning, setup | yes | yes, anywhere Python 3.11+ runs |
| Transcript import: upload, paste, folder, webhook, Fireflies, Fathom | yes | yes |
| Granola import, calendar | yes, needs the `claude` CLI | needs the `claude` CLI |
| Live capture (`callcap`) and live coaching | yes | **no** |
| Nudge overlay | yes (the overlay itself builds on macOS 13+) | **no** |
| Local transcription of recordings (`.[asr]`, MLX Whisper) | yes | **no**: MLX is Apple Silicon only |
| `launchd/` scripts | yes | no: run `salescoach serve` under your own service manager |

So far the project has only been run on macOS. Nothing outside capture, the overlay, MLX
transcription and launchd is macOS-specific by design, and CI is set up to run the test suite on
Linux as well, but treat Linux as unproven and Windows as untested. On a machine without capture,
bring calls in as transcripts from whatever recorder you already use.

## Hosted

The app can also run as one container on a host with a persistent volume (Fly.io, Railway,
Render), behind a password, for a seller who is not on the machine that runs it. `Dockerfile`,
`fly.toml` and the walkthrough are in [docs/deploy.md](docs/deploy.md), with the feature matrix:
the web app, the pipeline, transcript upload, the webhook, Fireflies and Fathom, and every API-key
model provider work hosted; live capture, the overlay, local transcription, the Claude CLI
provider, Granola, the calendar and (until an in-app OAuth flow exists) Gmail do not. Set
`SALESCOACH_PASSWORD` and `SALESCOACH_PUBLIC_URL`; without a password, `serve` refuses to bind to
anything but localhost.

## Your data stays on your machine

- Everything is stored under `data/` in the checkout (move it with `SALESCOACH_DATA`):
  `data/sales.db` (SQLite), `data/calls/<id>/` (audio, FLAC), `data/inbox/` (raw imported
  transcripts), `data/settings/` (your profile, style guide, choices). `data/` is gitignored.
- Secrets (API keys, the webhook secret) live in `data/settings/secrets.env`, written atomically
  with mode 0600. The setup pages show "set" or "not set", never a value, and keys are kept out
  of logs and error messages. An environment variable of the same name wins over the file.
- The server binds to `127.0.0.1`. There are no accounts and no telemetry.
- What leaves the machine:
  - **Calls to the model provider you chose.** Transcripts, deal context and drafts are sent to
    that provider for analysis. With Ollama, or an OpenAI-compatible server on your own network,
    that stays local too.
  - **Gmail and Google Calendar, only if connected**: sending the emails you approve, reading
    replies to emails the coach sent, reading your calendar (never writing to it).
  - **A recorder's API, only if you enable its adapter** (Fireflies, Fathom, Granola).
  - **Model downloads** from Hugging Face, only when you run `salescoach models pull`.

## Safety model

- **Nothing is emailed without a click.** Every draft waits for Send. The one code path that
  reaches Gmail has no LLM in it and sends exactly the bytes it is handed. A send is exactly-once:
  a deterministic Message-ID, and an unknown delivery outcome blocks a retry until Sent has been
  searched or you confirm it did not go out.
- **Auto-send ships off and needs two files changed.** `config/automation.yaml` has
  `auto_send: {enabled: false, dry_run: true}`, and `config/policy.yaml` has
  `email_policy: ALL_EMAILS_REQUIRE_APPROVAL`. Auto-send runs only when both are changed by hand,
  and even then only for narrow cases (hold time, daily cap, working hours, lint clean, approved
  contacts or low-risk nudges). The setup wizard does not offer it.
- **Evidence validation.** Model output is a proposal. Quotes are checked against the cited
  turns, on the right speaker's channel; a loose match never supports more than medium confidence;
  only an exact match may close or change an existing loop. Items below `medium` confidence, and
  the system's own recommendations, cannot feed an email until you confirm them.
- **The memory gate.** Every stored fact carries confidence and provenance. Your own input
  always wins; a value backed by explicit evidence is never overwritten by a weaker claim, and the
  disagreement is recorded as a conflict instead.
- **Recipients.** A draft may only address people on the call or the deal, plus addresses you
  type yourself.
- **Transcripts are data.** Prompts fence transcript and email text and tell the model it is not
  instructions; agents run with no tools.
- **Same-origin and Host checks** on every state-changing request; see [SECURITY.md](SECURITY.md).
- **The calendar is read-only**, enforced in code: the write tools are explicitly disallowed.

## Recording consent

Laws on recording calls differ by country and, in some countries, by state. Many places require
the consent of everyone on the call. **You are responsible for telling every participant that the
call is being recorded and for getting their consent before you record.** The app reminds you on
the Start form ("Tell everyone the call is being recorded, and get their OK, before you press
Start"), but it cannot do this for you and does not announce itself to the other side. The same
applies to a recorder whose transcripts you import. If someone does not agree, do not record.

## Status and known gaps

This is alpha software, generalised from a single-user tool. What is known not to be finished:

- **Gmail and calendar depend on the original author's local integrations.** Sending and reply
  polling read a Gmail OAuth client and token from `~/.gmail-mcp` (`credentials.json`,
  `tokens/<alias>.json`), which a separate private tool creates. The calendar is read through the
  `claude` CLI's Google Calendar connector. **A standard Google OAuth flow inside the app is the
  main missing piece for other users.** Until it exists, everything up to the draft works: copy
  the draft into your own mail client, and mark the call as sent or closed in the app. Without the
  calendar, `[SLOTS]` cannot be filled and blocks Send until you replace it with times yourself.
- **The Fireflies and Fathom adapters are untested against the live APIs.** They were written
  from the public API documentation and tested against fixtures. The JSON exports of both import
  through Upload regardless.
- **Single user by design.** One seller per install. On a laptop there is no login and the server
  listens on localhost only: **do not expose the port**; the only route meant to be reached from
  outside is `/import/webhook`, through a tunnel you run, protected by a shared secret. A
  [hosted install](docs/deploy.md) puts one password in front of everything; there is no
  multi-user mode, no roles and no MFA.
- **Live capture is macOS only.** There is no Windows or Linux capture.
- **Tuned on English and Hindi-English calls.** Other languages can be set in the profile; the
  live coach's cue lists and the language notes in prompts were only tuned for those two.
- **Some internal names are historical.** The methodology table is called `meddpicc` whatever the
  framework, and a few stored enum values and columns carry the original author's first name
  (`ask_<name>`, `needs_<name>`). They are never shown in the UI.
- **An optional bridge to a private task system** (`salescoach/integrations/jarvis_bridge.py`,
  `salescoach jarvis`) is inert unless that system's files exist on the machine.
- **Packaging.** Only editable installs from a clone are supported (see Quickstart).

[docs/review-2026-09-12.md](docs/review-2026-09-12.md) is the record of two safety reviews of the
core and what was fixed.

## Configuration

Two layers, merged on every read (no restart needed):

- `config/<name>.yaml` is tracked and generic: what every install starts from. Do not edit it
  for your own install.
- `data/settings/<name>.yaml` is yours, written by the setup pages (or by hand). It wins, key by
  key: mappings merge, a list or a scalar replaces the tracked value. `style.md` and
  `accounts.yaml` are replaced whole by your copy.

| File | What it holds |
|---|---|
| `seller.yaml` | your profile: name, emails, company, offering, languages, timezone, signature |
| `style.md` | the style guide the email drafters follow |
| `methodologies.yaml` / `methodology.yaml` | the built-in frameworks / your choice and custom ones |
| `models.yaml` | provider, per-provider settings and tier models, per-agent tier and effort |
| `sources.yaml` (settings only) | which transcript sources are on, polling, your speaker labels |
| `policy.yaml`, `automation.yaml` | email policy, follow-up run, reply polling, calendar, auto-send |
| `cadence.yaml`, `scheduling.yaml` | when an open loop comes due; how meeting times are proposed |
| `live_coach.yaml`, `asr.yaml`, `intel.yaml`, `learning.yaml`, `seller_taxonomy.yaml` | thresholds for the live coach, speech, deal intelligence, learning, and the selling-behaviour tags |
| `accounts.yaml` | optional bulk list of accounts, deals and people for `salescoach onboard` |

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `SALESCOACH_CONFIG` | `<checkout>/config` | the tracked defaults |
| `SALESCOACH_DATA` | `<checkout>/data` | database, recordings, inbox, settings |
| `SALESCOACH_SETTINGS` | `<data>/settings` | your settings and `secrets.env` |
| `SALESCOACH_RUNTIME` | `~/Library/Application Support/salescoach/runtime` | empty working folders for model CLI calls; set it on Linux |
| `SALES_DB` | `<data>/sales.db` | the database file alone |
| `WORLD_DB`, `JARVIS_DIR` | `~/.claude/jarvis/world.db`, `~/.claude/jarvis` | the optional private task-system bridge; ignored when the files are absent |
| `SALESCOACH_NO_PLUGINS` | unset | `1` runs the bare core without plugins |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `LLM_API_KEY`, `FIREFLIES_API_KEY`, `FATHOM_API_KEY`, `WEBHOOK_SECRET` | unset | secrets; the environment wins over `secrets.env` |
| `COACH_URL` | `http://127.0.0.1:8140` | where the overlay finds the server |
| `CALLCAP_SIGN_IDENTITY` | unset | code-signing certificate for `callcap/build.sh` |

## Daily workflow

1. **Before a call.** The Today page lists your meetings (when the calendar is connected). Press
   Record on the ones to capture; deal meetings get a prep brief. Create a deal first if there is
   none: on the Deals page, or `salescoach deal add "Acme pilot" --account "Acme" --domain acme.example`
   and `salescoach person add "Asha Rao" --email asha@acme.example --deal <deal-id> --role champion`.
2. **During.** An armed meeting starts recording a minute before it and stops 15 minutes after
   its end time, or press Start call / Stop yourself. Tell everyone first. Run
   `overlay/.build/release/coach-overlay` to see nudges over the meeting window. Not capturing?
   Let your recorder run and import its transcript afterwards.
3. **After.** Within a few minutes the call appears under "Calls to review". Confirm or reject
   the proposed loops, correct the stakeholder roles, edit the draft, then Send, save to Drafts,
   or close without an email. A failed step shows a "Retry from ..." button.
4. **Every morning.** Today shows loops due, replies mapped to loops for you to accept, and the
   follow-up review's decisions. Nudge drafts wait for your Send.
5. **Every so often.** The Learning page shows what the coach believes about how you sell;
   correct it there. The deal page is where you set stage, won or lost, and the reason.

Nothing runs while the server is stopped: the worker, the scheduler and the pollers all live
inside `salescoach serve`.

Command line (`salescoach <command> --help` for options):

| Command | Does |
|---|---|
| `serve [--port N] [--no-worker]` | web UI, worker and scheduler; `--no-worker` previews a database without acting on it |
| `status`, `work` | each call's state and queued events; drain the queue without the server |
| `call --title ...` | capture a call in the foreground; Ctrl-C ends it |
| `import-audio`, `import-text`, `import-file` | bring in a recording, a plain transcript, any recorder's export |
| `sources list\|poll\|webhook-secret` | transcript sources; `webhook-secret` prints the new secret once |
| `process CALL [--from STEP] [--force]` | re-run the pipeline for one call |
| `loops`, `deal add\|list`, `person add`, `onboard` | open loops, deals and people |
| `strategy`, `prep`, `coach-report`, `embed-index` | deal strategist, pre-call brief, selling report, local embeddings (Ollama) |
| `coach-replay CALL` | replay a finished call through the live coach |
| `followups`, `replies`, `calendar`, `autosend` | the follow-up review, reply polling, calendar reads, auto-send status |
| `learn [--recompute] [--show] [--digest]` | the learning layer |
| `models list\|pull\|pull-diarization` | speech models; downloads are always explicit |
| `eval` | run evaluation cases you keep under `data/eval` |

## Architecture

```
callcap/      Swift capture helper: microphone and system audio as two channels (vendors AudioTeeCore, MIT)
overlay/      Swift always-on-top panel that shows live nudges
config/       tracked default settings
launchd/      macOS launch agent template, install and uninstall scripts
salescoach/
  live/, speech/     frames, crash-safe audio archive, VAD, live and final transcription, diarization
  sources/           one import path (base.py), parsers, adapters per recorder
  agents/            the post-call agents and their prompts (agents/prompts/*.md)
  validators/        evidence, recipients, dates, confidence gates, voice lint
  memory/            the memory gate; selling-pattern counts
  orchestrator/      persistent event bus, per-call state machine, worker, review actions
  execution/         email policy, the Gmail path, follow-up cadence
  automation/        follow-up review, reply polling, read-only calendar, scheduler, auto-send executor
  intel/             methodology, deal strategist, prep brief, reconciler, selling report
  coach/             live coach: detectors, ranker, slow pass, replay
  learning/          outcomes, observations, learned patterns, feedback into prompts
  providers/         model providers behind one interface
  setupui/           the setup wizard, which is also Settings
  plugins/           how the phases attach to the core
  store/             SQLite schema, migrations, the event-emitting engine
  web/               FastAPI app, templates, static files
tests/        offline test suite
```

The seams meant for extension:

- **Plugins** (`salescoach/plugins/<name>.py` + `<name>.sql`): nav entries, a router, pipeline
  steps and event handlers, CLI commands, background duties. A plugin that fails to load is
  skipped and reported; it never takes the core down.
- **Providers** (`salescoach/providers/`): one protocol, `extract_structured` and `generate`.
- **Source adapters** (`salescoach/sources/adapters/`): produce a `NormalizedTranscript`; the
  import path does the rest.
- **Methodologies**: data in YAML, no code.

[docs/architecture.md](docs/architecture.md) has the pipeline, the event bus, the stores and
the safety checks in detail. [CONTRIBUTING.md](CONTRIBUTING.md) shows how to add each of the above.

## Tests

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

The suite is offline: no test reaches a model, the network, Gmail or the calendar. Providers
and services are replaced by fakes.

## Documentation

- [docs/install.md](docs/install.md): installing, permissions, keeping it running, upgrading, uninstalling
- [docs/setup.md](docs/setup.md): the setup wizard step by step
- [docs/methodologies.md](docs/methodologies.md): how a methodology is defined, and writing your own
- [docs/providers.md](docs/providers.md): model providers, tiers, keys
- [docs/sources.md](docs/sources.md): getting transcripts in from any recorder
- [docs/learning.md](docs/learning.md): what is learned, thresholds, how to correct it
- [docs/architecture.md](docs/architecture.md): pipeline, event bus, plugins, stores, safety checks
- [docs/design-setup-and-learning.md](docs/design-setup-and-learning.md): the design note behind the generalisation
- [docs/review-2026-09-12.md](docs/review-2026-09-12.md): two safety reviews and their fixes
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

## Licence

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE). `callcap/vendor/AudioTeeCore` is
MIT-licensed third-party code; see `callcap/vendor/LICENSE-audiotee.md`.
