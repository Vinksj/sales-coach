# Model providers

Every agent in the coach asks for structured output from "a model". Which vendor runs it is one
setting. No module outside `salescoach/providers/` knows about a vendor.

## Tiers

Agents do not name models. Each agent declares a **tier** in `config/models.yaml`:

| Tier | Agents |
|---|---|
| `heavy` | `call_analyst`, `actions`, `email`, `deal_strategist` |
| `light` | `quality`, `summary`, `live_coach`, `followup`, `nudge`, `reply_analysis`, `assessment_reconciler`, `longitudinal_coach`, `prep_writer` |

The active provider maps each tier to a model id (`providers.<name>.tiers`). Changing provider
moves every agent at once. An agent that `models.yaml` does not list (a plugin's) is `light`.

An agent entry may also carry `effort` (advisory; a provider maps it or ignores it), and may pin
`provider:` and/or `model:` for a mixed local and cloud setup. An explicit `model` beats the tier.

If the active provider has no model for a tier, the agent run fails with "no model chosen for
the ... tier; pick one in Setup". No request is sent.

## The providers

| Key | What it is | Key needed | Shipped tier models |
|---|---|---|---|
| `claude_code` | The `claude` CLI on this machine, on your Claude subscription. The shipped default | no; the CLI must be installed and logged in | `opus` / `sonnet` (CLI aliases) |
| `anthropic` | Anthropic Messages API | `ANTHROPIC_API_KEY` | `claude-opus-5` / `claude-sonnet-5` |
| `openai` | OpenAI Chat Completions, `https://api.openai.com/v1` | `OPENAI_API_KEY` | none: list the account's models and pick |
| `xai` | xAI, `https://api.x.ai/v1` | `XAI_API_KEY` | none: list and pick |
| `openai_compatible` | Any server that speaks Chat Completions (a gateway, vLLM, LM Studio). You give the base URL | `LLM_API_KEY`, optional for a local server | none |
| `ollama` | Ollama on this machine, `http://localhost:11434` | no | `qwen2.5:14b` for both tiers |

How structured output is obtained:

- **`claude_code`** runs `claude -p` from an empty sandbox folder under the runtime directory
  (`SALESCOACH_RUNTIME`) with `--setting-sources project`, so the model sees no user-level
  `CLAUDE.md`, no plugins, no MCP servers, no tools and no session history. The prompt goes in on
  stdin. A session or usage limit is reported as a rate limit.
- **`anthropic`** forces a single tool call whose `input_schema` is the agent's contract.
- **`openai`, `xai`, `openai_compatible`** use `response_format` `json_schema` in strict mode. An
  endpoint that rejects that gets `json_object` with the schema in the prompt instead; the
  provider remembers what an endpoint refused.
- **`ollama`** uses `/api/chat` with a JSON-schema `format`.

In every case the provider validates the answer against the agent's pydantic schema itself. An
invalid answer gets one retry with the validation error; after that the run fails and nothing
downstream is written. Errors are one of three kinds: rate limited (wait; never retried in a
loop, never sent to a fallback), schema violation, or provider error.

A note from use: small local models timed out on long call transcripts (about 20k tokens). The
`fallback` block in `models.yaml` exists, but no agent opts into it for that reason.

### Rules for a base URL

Only `openai_compatible` has an editable base URL. It must be `http` or `https` with a host and
no embedded credentials, and plain `http` is accepted only for `localhost`, `.local`,
`.localhost`, or a loopback or private IP address. HTTP providers follow no redirects and use an
explicit timeout on every request.

## Where keys are stored

- A key typed into Setup is stored with `config.set_secret` in `data/settings/secrets.env`
  (`KEY="value"` lines), written atomically with mode 0600. `SALESCOACH_SETTINGS` moves the folder.
- A secret is looked up in this order: the process environment, `data/settings/secrets.env`, then
  a legacy `secrets.env` in the checkout root (read only, never written).
- Which secret a provider uses (`api_key_env`) is fixed per provider and cannot be changed from a
  form, so a form post cannot point the bearer token at another environment variable.
- The key is read at call time and exists only in the request headers: not on the provider
  object, not in an exception, a log line or a stored result. Error text is scrubbed of the key
  and of anything key-shaped before it reaches the page.
- `models.yaml` never holds a key. Saving a provider choice is refused if the text to be written
  contains a stored key.

## What the Setup buttons do

**Load models** stores the key if you typed one, then lists model ids: `GET /models` on the
endpoint for the HTTP providers, `/api/tags` for Ollama, and the fixed aliases `haiku`, `opus`,
`sonnet` for the Claude CLI. Nothing else is saved.

**Test connection** stores the key if you typed one, then makes one small structured call ("reply
with the single word ok") against the **light** tier model, with the values currently in the
form, saved or not. It reports `ok`, the model that answered, the latency and, on failure, one
sentence. It never raises and never shows a traceback. The result is recorded (provider, ok,
model, latency, error, time) so the setup rail and the Today card know whether the active
provider has passed a test. It does not change which provider is active.

**Use this provider** makes the provider active and writes its tier models (and base URL, where
editable) to `data/settings/models.yaml`. It is refused until the provider can work: CLI present,
key stored, base URL filled, both tiers chosen.

## Editing by hand

`data/settings/models.yaml` overrides `config/models.yaml` key by key:

```yaml
provider: openai
providers:
  openai:
    tiers: {heavy: <a model id from Load models>, light: <another>}
agents:
  live_coach: {tier: light, effort: low, provider: ollama, model: "qwen2.5:14b"}   # one agent pinned locally
```

## Other things that use a model or the CLI

- **Calendar and Granola** go through the `claude` CLI's connectors whatever the model provider
  is. Without the CLI they are shown as unavailable.
- **Embeddings** for "what worked before" (`salescoach embed-index`) use Ollama
  (`nomic-embed-text`, `config/intel.yaml`), independent of the chat provider.
- **Speech-to-text** is local (MLX Whisper) and is not a provider setting; see `config/asr.yaml`.

## Adding a provider

See [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-model-provider).
