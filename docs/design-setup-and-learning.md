# Design: setup UI, pluggable everything, and a learning layer (2026-09-17)

Goal: any org can install the coach, run a setup UI, and get a coach that speaks their
methodology, uses their model provider, reads their recorder, knows their context, and
learns patterns over time. Today all of that is hard-wired to one seller.

Non-goals for this pass: multi-user/hosted deployment, auth, Windows/Linux live capture.
One instance = one seller on one machine.

## 0. Principles
- MEDDPICC + the current seller stay the DEFAULT BEHAVIOUR for the existing install: after
  every phase the full test suite passes and the original install behaves as before.
- Every choice in the setup UI must take effect in behaviour. No decorative settings.
- Nothing that sends email or closes loops becomes less safe. Send still needs a click.
- Numbers shown to the user are computed in code, never by a model.
- New features attach as plugins (salescoach/plugins/<name>.py + <name>.sql + own router)
  wherever possible, so they do not collide in web/app.py.

## 1. Settings layer (Phase A)  — salescoach/config.py
- `user_dir()` = env SALESCOACH_SETTINGS or `DATA_DIR / "settings"` (resolved at call
  time; tests monkeypatch DATA_DIR). Gitignored because data/ is.
- `load(name)` = deep-merge(tracked `config/<name>.yaml`, `user_dir()/<name>.yaml`); cache
  keyed on both mtimes. `save_user(name, data)` atomic YAML write. `text(name)` prefers the
  user file; `save_user_text(name, text)`.
- Secrets: `secret(key)` = env, then `user_dir()/secrets.env`, then legacy `ROOT/secrets.env`.
  `set_secret(key, value)` atomic, chmod 600, value never logged or echoed back.
  `has_secret(key)`. Delete the broken `reset_cache()`.
- The tracked `config/` ships generic. The original seller's identity, `style.md`, `accounts.yaml`,
  `my_addresses`, `own_domains`, `from_name` move to his `data/settings/` by a one-off
  migration script run in this phase (so that install's prompts keep its company context).

## 2. Seller profile + prompt templating (Phase A) — salescoach/seller.py
- `config/seller.yaml` (tracked, empty example) + user overlay. Fields: name, emails[],
  company, role, website, offering, icp, buyer_titles, own_domains[], languages[],
  timezone, signature, call_context (free text the user writes about how they sell).
- `profile()`, `is_configured()` (name + one email + company + offering), `first_name()`,
  `aliases()` (names a transcript may use for the seller), `language_note()` (derived
  sentence: e.g. calls mix English and Hindi and the recogniser mangles the Hindi).
- `render(text)`: regex substitution of `{{var}}` ONLY (prompts contain literal `{garbled}`
  markers, so never str.format); an unknown variable raises.
- Choke point: the four prompt readers (agents/base.py:36, intel/agentkit.py:38,
  automation/common.py:93, coach/slow_pass.py:86) and the three appenders all return
  `seller.render(...)`. `prompt_version`/`input_sha` hash the rendered text, so a profile
  edit invalidates caches correctly.
- Rewrite the 12 prompts + prompt-side Python strings (email_drafter.py:26,34,
  actions.py:19, history.py:209-212,326-329, pydantic Field descriptions in
  intel/schemas.py and automation/schemas.py) with variables and neutral pronouns.
- Python defaults read the profile: repo.ensure_me, automation/common.DEFAULT_ME /
  my_addresses, sources/granola.ME_EMAILS, onboard.INTERNAL_DOMAINS,
  jarvis_bridge.INTERNAL_DOMAINS, calendar own_domains, policy from_name + Message-ID
  domain fallback (policy.py:98), strategist._is_me, prep.py:51 owner label.
- Actor strings that carried the original seller's first name become "user" / "user:ui" (audit only; authority is
  `confidence == "user_input"`). KEEP the enum/column names `ask_<first name>`, `needs_<first name>`
  for now (DB values; a later migration) but no user-visible text may show them.
- Web: base.html wordmark/title from the profile's company (fallback "Sales Coach").
- First-run gate: until `seller.is_configured()`, every HTML GET except /setup*, /static*
  redirects to /setup; call creation refuses. Tests get a configured profile from conftest
  (the original seller's values, so existing assertions keep holding).
- Timezone: `common.IST` stays the default zone but is read from the profile's timezone;
  the literal "IST" labels become the zone's abbreviation. Do not attempt a full i18n pass.

## 3. Model providers (Phase B)
- `providers/http_chat.py`: `AnthropicProvider` (Messages API, forced tool use with
  input_schema; drop `effort`), `OpenAICompatProvider(base_url, api_key_env,
  structured="json_schema"|"json_object")` covering OpenAI, xAI (https://api.x.ai/v1) and
  any compatible endpoint; on a 400 for json_schema fall back to json_object with the schema
  in the system prompt. Schema transform for strict mode: strip `default`, all properties
  required. 429 -> RateLimited; 401/403/5xx/timeouts -> ProviderError with a clear message;
  bad output -> SchemaViolation(raw).
- Tiers replace Claude aliases: agents declare `tier: heavy|light` in config/models.yaml;
  `providers.<name>.tiers: {heavy: <id>, light: <id>}`. Remove the "sonnet" default
  (providers/__init__.py:38) and the `provider.name == "claude_code"` check (agentkit.py:24).
  claude_code tiers default to opus/sonnet; anthropic tiers to claude-opus-5 / claude-sonnet-5.
  OpenAI/xAI tiers have NO shipped default ids: the UI lists models from `GET /models` and the
  user picks.
- `providers.test_connection(provider_cfg)` runs one tiny extract_structured; `list_models()`.
- Granola + Calendar keep `claude -p` but are feature-gated on the CLI being present.
- HTTP via httpx with explicit timeouts, no redirects followed to other hosts, no key in logs
  or error text.

## 4. Methodology as data (Phase C) — salescoach/intel/methodology.py
- `config/methodologies.yaml` (tracked): meddpicc, meddic, bant, spiced, spin, challenger,
  sandler, command_of_the_message. Per element: key `[a-z0-9_]+` (not "health", not starting
  with "risk", no colon; reuse canonical keys across frameworks), label, known_when,
  partial_when, questions[], critical, cap_when_not_known, cap_rule, risk_when_unknown.
  Framework: name, lens, kind (qualification|conversation), health {min_known{count,cap,rule},
  rubric, bottleneck_hint}, coaching {analyst, secondary_lenses, live, live_weights}.
- User overlay `methodology.yaml`: {active: <key>, custom: {<key>: <definition>}}.
- `active()`, `available()`, `set_active(key)`, `validate_definition(d)`; MEDDPICC is the
  code-level fallback with today's keys, labels and cap rule names.
- The `meddpicc` table/name stays (internal name for "methodology elements"); rows for
  inactive keys are hidden and filtered out of history.user_meddpicc (history.py:162-166).
- Dynamic schema: `schemas.strategy_model(keys)` via pydantic.create_model; the class must
  still be NAMED `DealStrategy` (FakeProvider keys on the class name). Strategist fills
  `{{ELEMENTS}}`, `{{BOTTLENECK}}`, `{{HEALTH_RUBRIC}}` in deal_strategist.md.
- Fix the crashers: prep.py:32-33,74 GAP_ORDER.index, prep.py:122 label lookup,
  web.py:200 element check, analysis.Gap.lens Literal.
- On switch: queue STRATEGY_REQUESTED for open deals. Live-coach slots/triggers stay as is;
  only `coaching.live` text and optional weight overrides are methodology-aware.

## 5. Transcript sources (Phase D)
- `sources/base.py`: `NormalizedTranscript` (source_ref, title, started_at, ended_at,
  participants[{name,email}], turns[{speaker_label,text,t_start?,t_end?}], summary?, raw) and
  `import_normalized(conn, nt, deal_id=None, history=False)`: dedupe on source_ref, save raw
  under data/inbox/<kind>/, resolve people by email, map speakers, insert turns, publish
  CALL_ENDED. paste.import_text and granola.import_meeting are refactored onto it.
- Migration 4: rebuild `calls` without the `source` CHECK; add `calls.history` (replaces the
  `== "granola"` policy checks and TEXT_SOURCES; text-ness = no audio_dir).
- Speaker mapping is profile-driven: a label equal to the seller's name / alias / email
  local-part / "Me" -> channel me; everyone else -> them with the label as cluster and
  person_id pre-filled on an exact participant-name match. If NO label resolves to the
  seller, hold the import in `needs_speaker` state and ask "which speaker are you?" rather
  than guess (a wrong guess corrupts evidence validation).
- Parsers: plain "Name: text", Otter-style "Name  0:12" + next-line text, .vtt, .srt,
  Fireflies/Fathom JSON exports.
- Adapters, in this order: upload (any of the above), watched folder `data/inbox/drop/`,
  webhook `POST /import/webhook` (shared secret header, constant-time compare), Fireflies
  (GraphQL, API key), Fathom (REST, API key), Granola (existing, gated on the CLI).
  Otter/tl;dv/others: export -> upload/folder. Adapters that were not exercised against the
  real service are labelled "untested against the live API" in the UI and README.
- User overlay `sources.yaml`: [{kind, enabled, poll_minutes, options}], polled by the
  existing scheduler; deals mapped with onboard.deal_for_emails.

## 6. Setup UI (Phase E) — plugin `setup`
- `/setup` wizard, also reachable later as Settings: 1 You and your org, 2 How you sell
  (methodology picker with each framework's elements shown; custom builder), 3 Model
  (provider, key, tier models, Test connection), 4 Where calls come from (built-in capture
  status + recorder adapters + upload/folder/webhook), 5 Email and calendar status
  (read-only checks), 6 Review. Each step saves on its own; nothing is lost on back/forward.
- Keys are write-only in the UI: show "set" / "not set", never the value.
- Same origin/Host checks as every other POST.

## 7. Learning layer (Phase F) — plugin `learning`
Outcomes first; without them the system learns from the model's own opinion.
- Migration 5 + plugin SQL: `deal_stage_history`; deals get value, close_target, lost_reason.
  A stage/status editor on the deal page writes through the memory gate as user_input.
- `derived_outcomes` filled by SQL/code only: email replied within 5 business days; meeting on
  the calendar within 10 days of a sent email; loop closed by its due date; fact-based
  next-call advance (new stakeholder, buyer loop closed on time, element unknown -> known).
- `pattern_observations` (family, key, scope refs, polarity, evidence, outcome_ref, seller_id)
  and `learned_patterns` (family, key, scope, n_calls, n_deals, support, status, label,
  first_seen, last_seen, user_state). Counting is code, as memory/patterns.recompute is.
  seller_patterns is kept and mirrored as family `seller`.
- Families: seller behaviour (+ numeric series from final coach_state: talk share, questions,
  slots filled; live + stereo imports only), email voice (one candidate style rule per sent
  edit, active at 3 edits or on accept; rules not customer text go into email AND nudge
  prompts), follow-up effectiveness (counts only below n=20 per bucket; never fed to prompts
  before that), live-nudge usefulness per trigger (replays excluded; at n>=15 propose a
  config change for the user to accept), buyer personas/objections (observation-only until
  >=8 deals, >=3 per bucket).
- Promotion: emerging = >=3 calls AND >=2 deals AND >=30% of the window; established = >=6
  calls over >=3 deals, present in both halves of the window. Labels, never percentages;
  always show n. Dormant after absence from the last 10 analysed calls; retired after 20 or
  by the user; recurrence revives dormant, never user-retired (flag "returned").
- Tag hygiene: a `new:` tag close to an existing one proposes a merge; never auto-merges.
- Feedback: only active/established, max 3 per prompt, each with id and n; ids recorded in
  agent_runs.input_refs. Targets: prep writer, live coach priority, email + nudge drafters
  (style rules), strategist (persona priors, labelled as priors).
- "What the coach believes" page: every candidate/active pattern with evidence links and
  Confirm / Wrong / Retire / Merge into. User actions are user_input and outrank everything.
- No causal claim before 10 closed deals: until then co-occurrence with n.
- `seller_id` on the new tables now, so org-level merging of abstracted rows is possible later.
