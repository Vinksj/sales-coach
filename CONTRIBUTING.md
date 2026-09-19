# Contributing

Thanks for looking. This project handles other people's words, sends email on someone's behalf
and records calls, so a small number of rules are not negotiable. They are listed first.

## The rules that matter here

1. **Every fix gets a regression test.** Reproduce the bug in a test, watch it fail, then fix it.
   `tests/test_core_review_fixes.py` and `tests/test_review2_fixes.py` are what this looks like.
2. **No test may reach a real model or the network.** No Gmail, no calendar, no `claude`, no
   recorder API, no Hugging Face. Use `FakeProvider` (the `fake_llm` fixture),
   `httpx.MockTransport` for HTTP providers and adapters, and the injectable `gmail_factory`,
   `live_factory` and `hub` of `create_app`. A test must not read a real settings folder, a real
   `secrets.env` or a real database either; the fixtures in `tests/conftest.py` isolate all three.
3. **Nothing may send email without an explicit user action.** There is one path to Gmail,
   `execution/policy.approve_and_send`, and it is reached from a Send click (or from the auto-send
   executor, which ships disabled and needs two config files changed by hand). Do not add a second
   path, do not call it from a handler, and do not make auto-send easier to switch on.
4. **Numbers shown to users are computed in code.** Counts, rates, scores' caps, thresholds and
   dates come from SQL and Python. A model may propose a score that code then caps; it never
   supplies a count, a percentage or a date arithmetic result.
5. **Prompts go through `seller.render`, with `{{var}}` only.** Prompts contain literal
   single-brace markers (`{garbled}`), so never `str.format` or an f-string over prompt text. An
   unknown `{{variable}}` raises by design. No tracked prompt or config file may name a person, a
   company or a product, or use gendered pronouns for the seller; tests enforce this.
6. **No personal or customer data in fixtures.** Invent names and use reserved domains
   (`example.com`, `*.example`, `*.test`). Never commit a real transcript, a real email, a real
   address or a real company, even an anonymised-looking one.

Also expected: model output is a proposal that code validates (evidence, recipients, the memory
gate) before anything is stored; anything read from a transcript, an email or a web response is
data, never instructions; a secret's value never appears in a log line, an exception, a
template or a test assertion message.

## Development setup

```bash
git clone <repository-url> salescoach && cd salescoach
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Python 3.11 to 3.13. Install editable (`-e`): `config/` lives beside the package and is found
relative to the checkout. You do not need `.[asr]`, ffmpeg, the `claude` CLI, Ollama or a Mac to
run the tests. On macOS, `callcap/build.sh` and `overlay/build.sh` build the Swift helpers
(`swift build -c release` is what CI runs as a compile check).

To look at the app without touching your own data:

```bash
SALESCOACH_DATA=/tmp/sc-dev .venv/bin/salescoach serve --port 8141
```

`serve --no-worker` starts the UI with no worker and no scheduler, which is the safe way to look
at a copy of a real database.

## Running tests

```bash
.venv/bin/pytest -q                      # everything; about a minute
.venv/bin/pytest tests/test_methodology.py -q
.venv/bin/ruff check .                   # pyflakes and syntax errors only (F, E9)
```

CI runs both on Linux and macOS with Python 3.11 and 3.12.

## How to add things

### Adding a methodology

A built-in methodology is data: add a block under `methodologies:` in
`config/methodologies.yaml`. The format, every limit and a full example are in
[docs/methodologies.md](docs/methodologies.md). Reuse canonical element keys (`champion`,
`economic_buyer`, `decision_process`, `identify_pain`, `metrics`, `budget`, `situation`) wherever
the concept is the same. `methodology.validate_definition` must return no errors;
`tests/test_methodology.py` loads every shipped definition. Every `known_when` must be a fact
about the buyer.

### Adding a model provider

1. If the service speaks OpenAI Chat Completions, it is configuration, not code: add a block
   under `providers:` in `config/models.yaml` with `base_url` and `api_key_env`, a `CATALOG` entry
   in `salescoach/providers/setup.py` (label, whether a key is needed, the fixed `api_key_env`,
   notes), and a label in `providers/__init__.py: LABELS`.
2. Otherwise implement the `LLMProvider` protocol in `salescoach/providers/base.py`
   (`extract_structured`, `generate`), build it in `providers/__init__.py: build`, and add the
   catalog entry. Validate the answer with pydantic yourself and raise only `RateLimited`,
   `SchemaViolation` or `ProviderError`.
3. Rules: explicit timeouts, follow no redirects, read the key with `config.secret()` at call time
   and keep it out of the instance, exceptions (`raise ... from None`), logs and results. Do not
   ship default model ids you have not verified exist.
4. Tests use `httpx.MockTransport` (see `tests/test_providers_http.py`): success, 401, 429,
   5xx, a schema violation, and that the key never appears in an error.

### Adding a transcript source adapter

1. If the recorder can export or has a webhook, prefer documenting that; the upload, folder and
   webhook doors already exist.
2. For a polling adapter, subclass `Adapter` in `salescoach/sources/adapters/` and implement
   `configured()`, `list_recent(since)` returning `MeetingRef`s and `fetch(ext_id)` returning a
   `NormalizedTranscript`. Register the class in `adapters.classes()`. The poller owns dedupe,
   deal mapping, the import and per-source state; an adapter never writes the database.
3. Put the service's field names in one function in `sources/parsers.py`, so a correction is a
   one-place change.
4. Use `sources/adapters/_http.py` for HTTP. Store the key with `config.set_secret` under the
   adapter's `api_key_env`; never in `sources.yaml`.
5. Set `verified = False` unless you ran it against the live service, and say so in
   `docs/sources.md`. Test with `httpx.MockTransport` (see `tests/test_sources_adapters.py`):
   pagination, fetch, mapping, auth failure.
6. Never guess which speaker is the seller. The import path holds the call in `needs_speaker`
   when it cannot tell.

### Adding a plugin

Create `salescoach/plugins/<name>.py` and, if it needs tables, `<name>.sql` beside it
(`CREATE ... IF NOT EXISTS` only). Define any of `NAV`, `router`, `register(workflow)`,
`register_cli(subparsers)`, `start_background(db_path, stop)`; see
[docs/architecture.md](docs/architecture.md#plugins). Build the router lazily (module
`__getattr__`) so the CLI and the worker do not import the web layer. Handlers can be re-run on
retry, so they must be idempotent. Put your tables behind the memory gate with
`gate.register_table` if models can propose values for them. A plugin that fails to load must not
break the core; keep imports of optional dependencies inside functions. New non-Python files need
a glob in `pyproject.toml` `[tool.setuptools.package-data]`; a test checks this.

### Changing a prompt

Prompts are the `.md` files under `agents/prompts`, `intel/prompts`, `automation/prompts` and
`coach/prompts`. The hash of the rendered prompt is the cache key for agent steps, so a prompt
edit re-runs that step for every call that is processed again. Keep volatile text (timestamps,
exact counts) out of prompts for the same reason.

## Pull requests

- Small and focused. Say what changed and why, and how you tested it.
- Tick the checklist in the PR template; it repeats the rules above.
- Changes to anything under "The safety checks" in `docs/architecture.md` need a test that fails
  without the change.
- By contributing you agree that your contribution is licensed under the Apache License 2.0
  (section 5 of the licence).

## Reporting security problems

Not in a public issue. See [SECURITY.md](SECURITY.md).
