# Setup, step by step

The setup wizard lives at `/setup` and is also the Settings pages afterwards (the "Settings"
entry in the navigation). It has six steps. Each step saves on its own POST, so going back and
forward loses nothing, and every save goes through the module that owns the setting, so a choice
takes effect on the next read everywhere. There is no restart.

Only step 1 gates the app. Until the profile has a name, one email address, a company and an
offering, every page redirects to `/setup` and no call can be started or imported. Steps 2 to 5
ship with working defaults and never block anything.

The progress rail shows one of four states per step, computed from the settings files, the
presence of secrets and the state table (never from "this page was opened"): **done**,
**default** (works as shipped, nothing chosen yet), **attention**, **to do**.

Everything the wizard writes goes to your settings folder, `data/settings/` by default
(`SALESCOACH_SETTINGS` moves it). The tracked files in `config/` are never written.

## 1. You and your org

Writes `seller.yaml`, and `style.md` when you change the style guide.

| Field | Required | What it changes |
|---|---|---|
| Name | yes | How prompts refer to you; the From name on emails; one of the labels that mark a transcript speaker as you |
| Email addresses | yes, at least one | Your own addresses are you, never a buyer: they are excluded from recipients, a reply from one is never read as the buyer's, and the local part of each is a speaker label for you |
| Company | yes | Prompt context; the title shown in the app |
| What you sell (offering) | yes | The sentence every agent gets about what is being sold |
| Role, website, who you sell to, who the buyers usually are | no | Prompt context |
| Words your buyers use | no | Given to the email drafters (through the style guide) and the prep writer, so they write in the buyer's vocabulary |
| Company email domains | no, defaults to the domains of your addresses | Colleagues on these domains are internal: never matched to a deal account, and a meeting with only internal people is skipped by the polling adapters. Free-mail domains never count as internal |
| Other names (aliases) | no | Extra speaker labels that mean you in an imported transcript |
| Languages, main one first | no, default `en` | Adds a note to prompts about mixed-language calls and what the recogniser does to them. Only English and Hindi-English were tuned |
| Timezone | no, default `Asia/Kolkata` | When the daily follow-up run fires, how meeting times are proposed and labelled, how a timestamp without a zone is read |
| Email sign-off | no, defaults to your name | The exact sign-off the email drafter must use |
| How you sell | no | Free text appended to the seller context in prompts (deal size, cycle length, who joins your calls) |
| Style guide | no | Replaces the shipped `config/style.md` for the email and nudge drafters. Clearing it returns to the shipped guide |

Single-line fields refuse line breaks (the name goes into an email header). Prompts are hashed
after they are rendered with your profile, so editing the profile invalidates cached agent
results exactly as editing a prompt would.

## 2. How you sell

Writes `methodology.yaml` (`{active, custom}`).

Pick one of the built-in methodologies (MEDDPICC, MEDDIC, BANT, SPICED, SPIN, Challenger,
Sandler, Command of the Message) or build your own. Each card shows the framework's elements.
MEDDPICC is the default until you choose.

What the choice changes:

- the elements the deal strategist scores, and the elements shown on the deal page and in the
  prep brief;
- which unknown elements cap deal health, and at what number;
- what the call analyst and the live coach are told about how your team sells, and optionally
  the weights of live nudge triggers.

When you switch, a strategist run is queued for every open deal that already has an analysed
call, so deals are re-read against the new framework; the page tells you how many. Nothing is
deleted: rows of the previous framework are hidden, elements with a shared key (champion,
economic buyer, ...) carry over, and switching back shows the old rows again with your own edits
intact.

The custom builder takes a name, a key, a kind, a description and 2 to 12 elements (label, what
"known" means, what "partial" means, questions, whether it is critical and the health cap when it
is not known). A custom methodology that is active cannot be deleted; switch first. The fields
the form does not show are described in [methodologies.md](methodologies.md).

## 3. Model

Writes `models.yaml` (provider, its settings, the two tier models) and, for a key,
`secrets.env`.

Choose a provider, paste its API key if it needs one, press **Load models** to list what the
account offers, pick a model for the **heavy** tier and one for the **light** tier, press **Test
connection**, then **Use this provider**. Details per provider are in [providers.md](providers.md).

- The key field is write-only. The page shows "set" or "not set"; a stored key is never sent
  back to the browser, and an empty field keeps the stored key.
- "Use this provider" is refused until the provider can work: the Claude CLI is installed (for
  the subscription option), a key is stored (where one is needed), a base URL is filled in (for a
  custom endpoint) and both tiers have a model.
- The result of the last test is kept in the state table. Until a provider is usable and has
  passed a test, the Today page shows a small "Finish setting up" card, which you can dismiss for
  that provider.

What the choice changes: every agent asks for a tier, and the active provider says which model
that is, so all thirteen agents move at once.

## 4. Where calls come from

Writes `sources.yaml`, and `secrets.env` for an adapter's API key or the webhook secret.

- **Built-in capture** (macOS): status only; it is set up by building `callcap` and granting the
  macOS permissions.
- **Upload** and **watched folder**: on by default. The folder source takes an optional absolute
  path instead of `data/inbox/drop/`.
- **Webhook**: press the button to create the shared secret. It is shown once, in that response,
  and cannot be shown again; create a new one if you lose it. No secret means the webhook is off.
- **Fireflies, Fathom**: paste the API key, choose the polling interval, optionally "only
  meetings that map to a known deal", switch on. Both are labelled untested against the live API.
- **Granola**: available only while the `claude` CLI is installed.
- Recorders without an adapter are listed with instructions (export, then upload or folder).

What the choice changes: which adapters the scheduler polls, and how often. Everything an adapter
brings in runs the normal pipeline. [sources.md](sources.md) has formats, the webhook contract
and the rule for deciding which speaker is you.

## 5. Email and calendar

Read-only status. Nothing on this page talks to a service: it looks at files and at what earlier
runs left in the state table.

| Row | Ready when | Without it |
|---|---|---|
| Gmail | a Gmail OAuth client and a stored sign-in for the sending account exist under `~/.gmail-mcp` | drafts are still written; copy them into your own mail client |
| Calendar | the `claude` CLI is installed and its Google Calendar connector has been reached once | no meeting list, no armed recording, and `[SLOTS]` cannot be filled |
| Recording calls on this Mac (optional) | `callcap` is built and the server started with live capture | bring calls in from a recorder or a file |
| Speech-to-text models (optional) | every model `config/asr.yaml` names is in the local cache | needed only for audio; `salescoach models pull <repo>` downloads one |
| Claude CLI (optional) | the binary is found | needed for the calendar, Granola and the subscription model option |

The Gmail and calendar rows depend on integrations that are not part of this repository yet;
see "Status and known gaps" in the README.

## 6. Review

Shows every choice, what still runs on a default, and what is missing. Only a missing profile is
blocking. **Finish** records that setup is done and returns to the Today page. Everything stays
editable under Settings.

## Settings the wizard does not cover

Edit these by putting a file of the same name in your settings folder with only the keys you
want to change: `policy.yaml` and `automation.yaml` (email policy, follow-up run time, reply
polling, calendar scan, auto-send), `cadence.yaml`, `scheduling.yaml`, `live_coach.yaml`,
`asr.yaml`, `intel.yaml`, `learning.yaml`. Mappings merge key by key; a list or a scalar in your
file replaces the shipped one.
