# Installing Sales Coach

This is the full install guide: from a blank machine to a running, permitted, always-on install,
plus upgrading, moving, and uninstalling. The [README quickstart](../README.md#quickstart) is the
short version of the first section.

## What you need

| Requirement | Why | Required? |
|---|---|---|
| Python 3.11, 3.12 or 3.13 | the app | yes |
| git | to clone and to upgrade | yes |
| macOS 14.4 or later on Apple Silicon | live call capture, the nudge overlay, local transcription | only for those features |
| Xcode command line tools (`xcode-select --install`) | builds the capture helper and the overlay | only for live capture |
| `ffmpeg` (`brew install ffmpeg`) | importing audio recordings (.m4a, .mp3, .wav) | only for audio import |
| The `claude` CLI, signed in | using a Claude subscription as the model, the calendar, Granola | only for those |
| An API key for Anthropic, OpenAI, xAI, or an OpenAI-compatible gateway | the alternative to the Claude CLI | one model source is required |
| A Google account with Gmail | sending follow-ups and reading replies | no; drafts can be copied into any mail client |

Everything the app stores stays on the machine: a SQLite file and folders under `data/`, settings
under `data/settings/`, secrets in `data/settings/secrets.env` (mode 0600). The only traffic that
leaves the machine is to the model provider you choose, and to Google if you connect it.

## 1. Install on macOS

```bash
git clone <repository-url> salescoach
cd salescoach
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # the app, plus pytest and ruff
.venv/bin/pip install -e ".[asr]"      # local transcription of recordings; Apple Silicon only
```

`pip install -e .` (editable) is the supported method. The default config, the Swift helpers and
the launchd scripts live beside the Python package and are found relative to the checkout, so a
plain `pip install .` into site-packages does not ship them.

Start it:

```bash
.venv/bin/salescoach serve
```

Open <http://127.0.0.1:8140>. Until a profile exists every page leads to the setup wizard, which
takes five to ten minutes: who you are and what you sell, your sales methodology, the model
provider and its key, where your call transcripts come from, and a read-only status page.
[setup.md](setup.md) explains each step and what it changes.

The server listens on localhost only and has no login. Do not expose the port; it is one
person's tool on one machine. (A hosted install, with a password, is a different setup:
[deploy.md](deploy.md).)

## 2. Live capture on macOS (optional)

Skip this if you bring calls in as transcripts from a recorder you already use. Live capture needs
a signed helper binary and two macOS permissions.

```bash
callcap/build.sh          # builds and ad-hoc signs callcap/build/callcap
overlay/build.sh          # optional: the always-on-top nudge panel
```

Then trigger the permission prompts once: on the Today page press **Start call** with any title.
macOS asks for the **Microphone**; accept it. System audio has no prompt: open System Settings >
Privacy & Security > **Screen & System Audio Recording**, and enable "System Audio Recording Only"
for `callcap`. Press **Stop**. The call page shows sample counts for both channels; both above
zero means capture works. Delete that test call from its page.

Two things to know:

- An ad-hoc signature is tied to the exact binary. Every rebuild voids the grant, and you repeat the
  prompts. `callcap/build.sh` explains how to sign with your own certificate so a grant survives
  rebuilds.
- The app cannot see whether the grant exists. If a recording is silent, the grant is the first
  thing to check.

Run the overlay alongside the server during calls:

```bash
overlay/.build/release/coach-overlay
```

## 3. Keep it running (macOS)

The scheduler (follow-ups, reply polling, calendar reads, armed recordings, recorder polling) runs
inside `salescoach serve`, so nothing happens while it is stopped. To start it at login and restart
it if it dies:

```bash
sh launchd/install.sh
```

This generates a launch agent for your checkout, your user, and the tools found on your machine
(`claude`, `ffmpeg`, `ollama`, each optional), and starts it. The log is
`~/Library/Logs/salescoach/serve.log`. Environment variables set in your shell when you run it
(`SALESCOACH_DATA`, `SALESCOACH_SETTINGS`, `SALES_DB`, `SALESCOACH_PORT` and the others in the
script's header) are written into the agent, so the service uses the same folders your shell does.

Useful commands afterwards:

```bash
launchctl kickstart -k gui/$(id -u)/com.salescoach.serve   # restart (after an upgrade)
tail -f ~/Library/Logs/salescoach/serve.log                # watch it
.venv/bin/salescoach status                                # calls, states, queued work
sh launchd/uninstall.sh                                    # stop it and remove the agent
```

## 4. Install on Linux or another macOS version

The web app, the pipeline, deals, loops, prep briefs, learning, and every transcript import path
run anywhere Python 3.11+ runs. Live capture, the overlay, and MLX transcription do not.

```bash
git clone <repository-url> salescoach
cd salescoach
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/salescoach serve
```

Set `SALESCOACH_RUNTIME` to a writable folder (the default is a macOS path):

```bash
export SALESCOACH_RUNTIME=~/.local/share/salescoach/runtime
```

Run it under your own service manager (a systemd user unit that runs
`/path/to/salescoach/.venv/bin/salescoach serve` with `WorkingDirectory` set to the checkout is
enough). Bring calls in through upload, the watched folder, the webhook, or a recorder adapter;
see [sources.md](sources.md). Linux is unproven in production and Windows is untested; the test
suite runs on Linux in CI.

To run it on a server instead, as a container with a persistent volume behind a password (Fly.io,
Railway, Render), see [deploy.md](deploy.md).

## 5. Verify the install

```bash
.venv/bin/pytest -q            # the full suite, offline, no model calls
.venv/bin/salescoach status    # should list no calls and no queued events on a fresh install
```

In the browser, Settings > **Email and calendar** shows what the app can reach: Gmail, the
calendar, the capture helper, speech models, the Claude CLI. Settings > **Model** > **Test
connection** makes one small model call and reports the latency, or the error.

## 6. Upgrade

```bash
cd salescoach
git pull
.venv/bin/pip install -e ".[dev]"                          # picks up new dependencies
launchctl kickstart -k gui/$(id -u)/com.salescoach.serve   # or restart salescoach serve by hand
```

Database migrations run on the first connection after an upgrade and are transactional; a failed
migration leaves the previous version intact. Your settings in `data/settings/` are never touched
by an upgrade; the tracked defaults in `config/` are. If a settings file becomes unreadable, the
app falls back to the defaults and shows a banner naming the file and line.

Rebuild the capture helper only when `callcap/` changed (`git log --oneline -- callcap` after a
pull). Remember the permission grant is lost on rebuild.

## 7. Move to another machine

Copy the checkout and the `data/` folder (the database, the settings, the secrets file, the raw
transcripts and recordings). Recreate the virtual environment on the new machine, rebuild the
capture helper, and grant the permissions again. Never copy `data/` anywhere shared: it holds your
customers' calls.

## 8. Uninstall

```bash
sh launchd/uninstall.sh          # macOS: stop the service and remove the launch agent
```

Then delete the checkout. `data/` inside it holds everything the app knew, including recordings and
transcripts; delete it deliberately. The permission grants for `callcap` disappear with the binary.
Nothing is installed outside the checkout except the launch agent, the log folder
`~/Library/Logs/salescoach/`, and the runtime folder (`~/Library/Application Support/salescoach/`
on macOS, or wherever `SALESCOACH_RUNTIME` pointed).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every page redirects to `/setup` | no profile yet, or `data/settings/seller.yaml` is broken (a banner names the line) | complete step 1, or fix the file |
| A recording is silent on one or both channels | the macOS grant is missing, or the helper was rebuilt | repeat the permission steps in section 2 |
| "No worker is running" in `salescoach status`, events stay pending | the server is not running, or it was started with `--no-worker` | start `salescoach serve`, or run `salescoach work` once |
| "Refused: unknown host" on a request | the request did not come from `127.0.0.1:8140` in a browser | use the address the server printed; do not put it behind a proxy or tunnel |
| The webhook returns 403 | the secret is missing or wrong, or the caller is not local and remote access is off | create the secret in Settings > Where calls come from; switch on remote access only behind a tunnel that forwards `/import/webhook` alone and keeps the Host header |
| Model calls fail with a key error | wrong key, or the key was stored for a different base URL | paste the key again in Settings > Model and press Test connection |
| The calendar never reads | the `claude` CLI is not installed or not signed in, or its calendar connector is not authorised | sign in to the CLI and authorise Google Calendar in your Claude settings |

If something else is wrong, `~/Library/Logs/salescoach/serve.log` (or the terminal running
`serve`) shows the traceback, and `.venv/bin/salescoach status` shows what is queued and what
failed.
