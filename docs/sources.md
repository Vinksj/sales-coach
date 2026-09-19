# Transcript sources: getting a call in from any recorder

There are hundreds of call recorders. The coach does not try to integrate with each one. It has
**one import path** (`salescoach/sources/base.py: import_normalized`) and several doors into it:

| Door | Kind | What you do | Works for |
|---|---|---|---|
| Upload | push | Import page > "Upload a transcript from any recorder" | every recorder that can export |
| Paste | push | Import page > paste box, or `salescoach import-text` | anything you can copy |
| Watched folder | poll, every minute | save files into `data/inbox/drop/` | exports, "save to folder" automations |
| Webhook | push | `POST /import/webhook` with a shared secret | Zapier, Make, n8n, a recorder's own webhook |
| Fireflies.ai | poll (API key) | set `FIREFLIES_API_KEY`, enable | Fireflies. **Untested against the live API** |
| Fathom | poll (API key) | set `FATHOM_API_KEY`, enable | Fathom. **Untested against the live API** |
| Granola | poll (claude CLI) | enable; needs the `claude` CLI with the Granola connector | Granola |
| Live capture / recording | | Start call, or Import > Upload a recording | not a transcript source; unchanged |

`salescoach sources list` shows every source, whether it is on, whether it is ready, and its last error.

## What happens to every transcript

1. **Dedupe.** Each transcript has a `source_ref`. The same one never becomes two calls:
   `paste:<digest of the parsed turns>`, `file:<digest of the file>` (upload and folder share it),
   `fireflies:<id>`, `fathom:<recording id>`, `granola:<id>`, `ext:<source>:<id>` for generic JSON
   that carries an `id`, else `webhook:<digest of the body>`.
2. **The raw payload is kept** under `data/inbox/<kind>/` before anything interprets it.
3. **People.** Participants with an email are found or created (with the account their domain
   belongs to) and linked to the deal. Your own address is you, never a buyer.
4. **Who is the seller?** See below. This decides the `me` / `them` channel of every turn.
5. **Turns** are stored with timestamps when the format has them. Text imports enter the pipeline at
   `diarized` (nothing to transcribe: "text" simply means the call has no `audio_dir`).
6. `CALL_ENDED` is published once and the normal pipeline runs: quality, summary, analysis,
   actions, loops, follow-up draft.

A recorder's own AI summary, when present, is stored as a `<kind>_summary` artifact labelled
third-party inference. It is never treated as fact.

**History.** `calls.history = 1` means "an old meeting, brought in for memory": it is analysed, but
no follow-up email is drafted and Jarvis is not told the call belongs to sales. Only explicit
backfills set it (`salescoach backfill-granola`, `salescoach import-file --history`). Everything a
recorder adapter, the folder, the webhook or an upload brings in is a normal call, so a follow-up
is drafted (subject to the usual 48-hour staleness rule).

## Which speaker are you?

Recorders label speakers with real names. Channels matter: the evidence validator accepts *your*
explicit commitment only when the quoted words are on your channel, and talk share is per channel.
A label is you when it equals, ignoring case, spaces and punctuation:

- your profile name, first name, or an alias (`seller.yaml: aliases`),
- the local part of one of your email addresses,
- `Me`,
- the name of a participant whose email is yours (Fathom and Fireflies tie speakers to addresses),
- a label you picked before (remembered in `sources.yaml: me_labels`),
- the label you gave for this import ("Your name in this transcript", or `--me`).

If a transcript has two or more speakers and none of them is recognisably you, **the coach does
not guess**. The call is created in state `needs_speaker`, nothing processes it, and the call page
(and the Today page) asks "Which speaker are you?". Your answer fixes the channels, starts the
pipeline and is remembered, so the same recorder does not ask again. "None of them" is a valid
answer. Generic labels (`Speaker 2`) are asked every time, because they mean someone else in the
next meeting. A transcript with no speaker names at all is refused with an explanation.

## File formats (Upload, Folder, `salescoach import-file`)

The format is sniffed from the content; the extension is only a hint. Up to 5 MB.

**Plain text**: one turn per line, continuation lines allowed.

    Asha Rao: Our CFO will want the savings by site.
    [00:01:12] Dr. A. K. Sharma: And by lane.
    Mary-Jane O'Neil (Acme): Agreed. Note: finance signs off first.

Names may have up to five words, initials, dots, hyphens, apostrophes and non-Latin letters.
A label is only recognised at the start of a line (so "Note: ..." inside speech does not split a
turn), and document headers (`Date:`, `Attendees:` ...) are not speakers.

**Otter text export**: name and time on one line, the words below.

    Asha Rao  0:12
    Good afternoon. Can you hear me?

**WebVTT (.vtt)**: Zoom, Teams, Meet. Speakers from `<v Name>` voice tags or a `Name: ` prefix.
**SubRip (.srt)**: speakers from a `Name: ` or `[Name]` prefix.
Caption cues by the same speaker are merged into turns of at most 600 characters.

**Fireflies JSON** (an export, or the API's transcript object: `sentences[]` with `speaker_name`,
`text`, `start_time`, `end_time`). **Fathom JSON** (`transcript[]` with `speaker.display_name`,
`text`, `timestamp`).

**Generic JSON: THE schema** for the webhook, the folder and anything you script yourself:

```json
{
  "id": "meeting-7781",
  "source": "otter",
  "title": "Acme discovery",
  "started_at": "2026-09-16T15:00:00+05:30",
  "ended_at": "2026-09-16T15:42:00+05:30",
  "summary": "optional: the recorder's own summary (stored as third-party inference)",
  "participants": [{"name": "Asha Rao", "email": "asha.rao@acme.example"},
                   {"name": "Priya Shah", "email": "priya@yourcompany.example"}],
  "turns": [
    {"speaker": "Asha Rao", "text": "We lose two days on every detention dispute.", "start": 3.0, "end": 9.5},
    {"speaker": "Priya Shah", "text": "I'll send a one-page plan by Friday.", "start": "0:10", "end": "0:15"}
  ]
}
```

Only `turns[].speaker` and `turns[].text` are required. `start` / `end` are seconds or
`h:mm:ss`. `started_at` without a zone is read in your profile's timezone. `id` (with `source`)
makes re-delivery idempotent; without it the body's digest is used. A turn may carry
`"channel": "me" | "them"` when the sender already knows (a recorder that separates microphone
from speaker audio); then no name matching is needed. Unknown fields are ignored: the payload can
not choose a deal, mark history or trigger anything. The deal is inferred from participant domains.

## Watched folder

Default `data/inbox/drop/` (another folder: option `path` of the `folder` source). Each `.txt`,
`.vtt`, `.srt`, `.json`, `.md` file that has been still for a few seconds is imported once, then
moved to `drop/processed/`. A file that cannot be imported moves to `drop/failed/` with a
`<name>.error` note beside it saying why. Dot-files, symlinks and other extensions are left alone.

## Webhook

    POST http://127.0.0.1:8140/import/webhook
    X-Salescoach-Secret: <secret>
    Content-Type: application/json
    <generic JSON>

- `salescoach sources webhook-secret` creates the secret and prints it **once** (it is stored in
  your settings folder's `secrets.env` as `WEBHOOK_SECRET`, mode 600).
- **No secret set = the webhook is off** (403). Wrong secret = 403. Body over 5 MB = 413. Not a
  transcript = 422. Created = 201 `{call_id, created, needs_speaker}`; a repeat = 200, same call.
- The comparison is constant-time. Every other POST in the app must come from the app's own pages
  (same-origin guard). The webhook is the single exception, and only for a `POST` to exactly
  `/import/webhook` that carries the valid secret. With that secret the Host check is skipped too,
  because a tunnel presents its own host name. The secret opens no other route.
- The coach listens on this machine only. A cloud automation (Zapier, Make) can reach it only
  through a tunnel you run (cloudflared, ngrok, Tailscale Funnel) pointed at port 8140. Through the
  tunnel, everything except an authenticated webhook call is refused.

## A recorder that has no adapter

Otter, tl;dv, Zoom, Google Meet, Teams, Gong and the rest have no adapter, on purpose: they have
no documented public transcript API (or only an enterprise-admin one), and nothing is invented.

1. **Export, then Upload or Folder.** Every one of them exports `.txt`, `.vtt`, `.srt` or JSON.
2. **Automation, then Folder.** A "new transcript" trigger that saves the file into a synced
   folder; set the folder source's `path` option to it.
3. **Automation, then Webhook.** Zapier / Make: trigger "new transcript", action "POST" the generic
   JSON above with the secret header.

## API adapters and what "unverified" means

`fireflies` (GraphQL, `https://api.fireflies.ai/graphql`, bearer key) and `fathom` (REST,
`https://api.fathom.ai/external/v1`, `X-Api-Key`) were written from the services' public API
documentation and tested only against fixtures of the documented shape (httpx MockTransport):
listing with pagination, fetching, mapping, auth failures. **They have not been run against the
live services.** Their descriptors say `verified: false`, and the UI must show "untested against
the live API". If one fails, its error is on `salescoach sources list`, the other sources keep
working, and the same recorder's JSON export still imports through Upload. The field names each
service uses live in exactly one function per service (`parsers.fireflies_to_parsed`,
`parsers.fathom_to_parsed`), so a correction is a one-place change.

`granola` wraps the existing `claude -p` connector path and is available only while the claude
CLI is installed. Each check is a short Claude session, so it polls hourly by default.

Polling: a newly enabled adapter looks back 2 days, then from its last successful poll minus 6
hours. At most 25 meetings per poll. Meetings whose other participants are all on your own company
domains are skipped. Option `only_deals: true` imports only meetings that map to a known deal.

## Settings (for the setup UI)

The user overlay `sources.yaml`:

```yaml
sources:
  - {kind: folder, enabled: true, poll_minutes: 1, options: {}}
  - {kind: fireflies, enabled: true, poll_minutes: 15, options: {only_deals: true}}
me_labels: ["Priya S."]
```

```python
from salescoach import sources, config
sources.catalog(conn=None) -> list[dict]
sources.save(kind, enabled, poll_minutes=None, options=None) -> dict   # the saved source's descriptor
sources.new_webhook_secret() -> str                                    # shown once
config.set_secret(descriptor["api_key_env"], value)                    # keys never go in sources.yaml
```

A descriptor: `{kind, label, how, mode: push|poll|export, needs_key, api_key_env, verified,
configured, enabled, poll_minutes, options, last_run, last_error}`. `mode: export` rows are the
recorders without an adapter (instructions only, nothing to save).

The scheduler duty `sources` (plugin `salescoach/plugins/sources.py`) ticks every minute and polls
whichever enabled, configured adapters are due. Per-source state: `sources:<kind>:last_run`,
`last_ok`, `last_result`, `last_error`. One adapter failing never stops the others.
