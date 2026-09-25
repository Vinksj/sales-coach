# Deploying Sales Coach as a hosted install

The app was built to run on one person's Mac. This page is for the other case: one instance, one
seller, reachable over the internet, running on its own without that Mac. It is a container with
one persistent volume, behind one password, on a host that keeps a process running. Fly.io is the
walkthrough; Railway and Render take the same container.

## SQLite or Postgres

A single-seller hosted install keeps the volume and SQLite exactly as described here. Anything
multi-user (a team, managers reviewing reps' calls: coming) needs Postgres and two roles: set
`DATABASE_MIGRATE_URL` to the owner role's `postgresql://` URL (the role that owns the schema and has
`BYPASSRLS`; it runs `salescoach migrate` before the first start and after every upgrade, and
`salescoach migrate --check` says whether the database is behind the build) and `DATABASE_URL` to
the app role's (`salescoach_app`: `CREATE ROLE salescoach_app LOGIN PASSWORD '…' NOSUPERUSER
NOBYPASSRLS NOCREATEDB NOCREATEROLE; GRANT CONNECT ON DATABASE … TO salescoach_app;` once, before the
first migrate, which grants it the tables). The app then serves from that database, as that role,
under row-level security, instead of `/data/sales.db`. The volume is still needed for settings,
secrets and imported transcripts. See "Two backends" and "Isolation" in [architecture.md](architecture.md).

## Why not Vercel

Vercel runs functions that start per request and end after it, on a filesystem that does not
persist; Sales Coach is one long-lived process (the web server, the workflow worker and the
scheduler are threads inside it) with a SQLite database and imported transcripts on disk. It
needs a host that runs a container continuously with a volume attached.

## What works hosted, and what does not

| Feature | Hosted | Notes |
|---|---|---|
| Web app, setup wizard, deals, loops, prep briefs, coach report, learning | yes | |
| The pipeline: quality, summary, analysis, actions, reconciliation, email draft | yes | |
| Transcript upload and paste, the watched folder | yes | the folder is `/data/inbox/drop` on the volume; upload is the practical path |
| Webhook (`POST /import/webhook`) | yes | switch on **Allow requests through a tunnel** in Settings: every hosted request arrives through the platform's proxy, so none counts as local |
| Fireflies and Fathom polling | yes | API keys in Settings, as on a laptop |
| Model providers with an API key: Anthropic, OpenAI, xAI, OpenAI-compatible gateways | yes | choose one in the setup wizard |
| Audio upload (`.m4a`, `.mp3`, `.wav`) | decodes only | `ffmpeg` is in the image, but there is no local transcription: upload transcripts, not recordings |
| Live capture, the live coach, the nudge overlay | **no** | they record a Mac's audio |
| Local transcription (MLX Whisper), speaker separation | **no** | Apple Silicon only |
| The `claude_code` provider (a Claude subscription through the CLI) | **no** | the CLI is not in the container |
| Granola, the calendar | **no** | both read through the Claude CLI |
| Ollama | **no** | unless you run one the container can reach and set its base URL |
| Gmail: Send, Save to Drafts, reply polling | cloud mode only | a single-seller hosted install shows "Not available" (it needs a sign-in stored on the machine); copy each draft into your own mail, then mark the call as sent. In cloud mode each rep connects their own Gmail with Google OAuth: [deploy-cloud.md](deploy-cloud.md). |

The setup wizard's last step says the same, row by row, on a hosted install.

## How it is put together

- `Dockerfile`: `python:3.12-slim`, `ffmpeg`, the checkout installed editable at `/app` (config/
  and the other folders are found beside the package, exactly as on a laptop), an unprivileged
  user `salescoach` (uid 1000), port 8140, a health check on `/health`.
- `docker-entrypoint.sh`: the image starts as root only long enough to make the volume writable
  by `salescoach` (platforms mount a fresh volume owned by root), then drops privileges with
  `setpriv` and runs `salescoach serve --host 0.0.0.0 --port $PORT`. If the platform already
  runs the container as a non-root user, it just execs.
- One volume at `/data`: `SALESCOACH_DATA=/data` and `SALESCOACH_RUNTIME=/data/runtime`, so the
  database (`/data/sales.db`), settings (`/data/settings/`), secrets saved from the wizard
  (`/data/settings/secrets.env`), imported transcripts (`/data/inbox/`) and the model sandboxes all
  live on it. The app creates the folders on first run.
- Authentication: with `SALESCOACH_PASSWORD` set, every request needs a session except `/login`,
  `/logout`, `/static/*`, `/health` and the webhook (which has its own secret). Pages redirect to
  `/login`; JSON and event streams get a 401. The session cookie is signed (HMAC-SHA256), HttpOnly,
  SameSite=Lax, Secure over https, valid thirty days, and re-issued on every login. Five wrong
  passwords from one address lock that address out for fifteen minutes.
- Proxy awareness: with `SALESCOACH_PUBLIC_URL` set, the platform's proxy is trusted for
  `X-Forwarded-*` (uvicorn rewrites the client address and scheme from them), and the same-origin
  guard checks `Host` and `Origin` against that URL, scheme, host and port. On a laptop the rule
  "refuse any request that carries a forwarding header" stays; hosted, every request carries one.
- `salescoach serve --host 0.0.0.0` refuses to start unless a password is set (or
  `--allow-unauthenticated` is passed, for a private network you trust).

### Environment variables

| Variable | Required | What it does |
|---|---|---|
| `SALESCOACH_PASSWORD` | one of these two | the login password; set it as a platform secret |
| `SALESCOACH_PASSWORD_HASH` | | the same as `pbkdf2_sha256$<iterations>$<salt>$<hex>`, so the platform never holds the password itself; wins when both are set. How to make one is just below the table. |
| `SALESCOACH_PUBLIC_URL` | yes | `https://<app>.fly.dev` or your own domain: what the seller types in the browser. Turns proxy mode on. |
| `SALESCOACH_SESSION_SECRET` | no | signs session cookies; unset, it is derived from the password (hash), so changing the password logs every browser out |
| `SALESCOACH_TRUST_PROXY` | no | uvicorn's `forwarded_allow_ips`; default `*` in proxy mode (the platform owns the network between its proxy and the container) |
| `SALESCOACH_DATA` | yes | `/data`, the volume |
| `SALESCOACH_RUNTIME` | yes | `/data/runtime` |
| `PORT` | no | what the container listens on; 8140 unless the platform sets it (Render does) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, ... | one model key | the provider's key. Secrets set in the environment win over `/data/settings/secrets.env`; you can also paste the key in the wizard instead, and it is saved on the volume. |
| `WEBHOOK_SECRET` | no | switches the transcript webhook on; or create it in Settings |
| `FIREFLIES_API_KEY`, `FATHOM_API_KEY` | no | or paste them in Settings |

To make a `SALESCOACH_PASSWORD_HASH`, from a checkout with the app installed (the password is read
from stdin, never from an argument, so it stays out of the shell history and the process list):

```bash
printf '%s' 'the password' | .venv/bin/salescoach password-hash
```

or with Python alone, on any machine:

```bash
python3 -c 'import hashlib, secrets, sys; s = secrets.token_hex(16); print("pbkdf2_sha256$600000$" + s + "$" + hashlib.pbkdf2_hmac("sha256", sys.stdin.readline().rstrip("\n").encode(), s.encode(), 600000).hex())'
```

(type the password, press Enter; the line printed is the value to set).

## Fly.io, step by step

You need a Fly account, `flyctl`, and a clone of the repository on your machine (Fly builds the
image from your checkout; nothing is pulled from a registry).

1. Install flyctl and sign in:

   ```bash
   curl -L https://fly.io/install.sh | sh      # macOS or Linux; brew install flyctl also works
   fly auth login
   ```

2. In the checkout, edit the two placeholders at the top of `fly.toml`: `app` (the name becomes
   the host, `https://<app>.fly.dev`) and `primary_region` (`fly platform regions` lists them; pick
   the one nearest the seller). Then create the app without deploying:

   ```bash
   cd salescoach
   fly launch --no-deploy --copy-config --name <app> --region <region>
   ```

   Answer no to any offer of a Postgres or Redis database; the app needs neither.

3. Create the volume in the same region. The name must match `[[mounts]] source` in `fly.toml`:

   ```bash
   fly volumes create salescoach_data --region <region> --size 3
   ```

   One volume, one machine: SQLite has one writer and the scheduler must run exactly once. Do not
   scale to more than one machine.

4. Set the secrets. They are stored encrypted by Fly and appear to the app as environment
   variables; they are never in `fly.toml` or in git.

   ```bash
   fly secrets set \
     SALESCOACH_PASSWORD='a long passphrase' \
     SALESCOACH_PUBLIC_URL='https://<app>.fly.dev' \
     ANTHROPIC_API_KEY='sk-ant-...' \
     WEBHOOK_SECRET="$(openssl rand -hex 24)"
   ```

   Use `OPENAI_API_KEY`, `XAI_API_KEY` or your gateway's key instead of the Anthropic one if that is
   the provider you will pick. `WEBHOOK_SECRET` is only needed if a recorder or an automation will
   post transcripts.

5. Deploy:

   ```bash
   fly deploy
   fly status                       # one machine, started
   curl https://<app>.fly.dev/health
   ```

   `/health` answers `{"status":"ok","version":"0.1.0","db":"ok","worker":"running","configured":false}`
   before the wizard has run.

6. First run. Open `https://<app>.fly.dev`, log in with the password, and the setup wizard opens.
   In step 3 (Model) choose the provider whose key you set, or paste a key, and press **Test
   connection**. In step 4 (Where calls come from) switch on the sources you will use; for the
   webhook, tick **Allow requests through a tunnel** (hosted, every request arrives through Fly's
   proxy). Step 5 lists Gmail, calendar, recording, speech models and the Claude CLI as not
   available in a hosted install, which is expected.

7. Bring in a call: Import > upload a transcript file, or point the recorder's automation at
   `https://<app>.fly.dev/import/webhook` with the `X-Salescoach-Secret` header.

Useful afterwards: `fly logs` (the server log), `fly ssh console` (a shell in the container; the
data is under `/data`), `fly machine restart`.

## Railway and Render

Both consume the `Dockerfile`; the container is the same, and so are the environment variables.

**Railway.** Create a project from the GitHub repository; `railway.json` tells it to build the
Dockerfile and to check `/health`. Attach a volume to the service in the dashboard with mount path
`/data` (volumes are not declared in `railway.json`). Set the variables from the table above in
the service's Variables tab; `SALESCOACH_PUBLIC_URL` is the public domain Railway generates (or
your own), with `https://`. Railway sets `PORT`; the container listens on it. If the volume is not
writable by the app user, set `RAILWAY_RUN_UID=0` so the entrypoint starts as root and can make it
so, or check Railway's current volume-permission guidance.

**Render.** `render.yaml` describes one Docker web service with a persistent disk at `/data` (a
disk needs a paid instance type and pins the service to one instance, which is right for this
app). Create a Blueprint from the repository, and fill in the secrets it asks for
(`SALESCOACH_PASSWORD`, `SALESCOACH_PUBLIC_URL`, the model key, optionally `WEBHOOK_SECRET`).
Render sets `PORT`; the container listens on it.

Anything else that runs a container with a persistent volume works the same way: mount the volume
at `/data`, set the variables in the table, expose the container's port through the platform's
https proxy, and put the public URL in `SALESCOACH_PUBLIC_URL`.

## Backing up `/data`

Everything is on the volume: the database, the settings, the secrets file, the raw transcripts.
Two ways, on Fly:

- Copy the folder down. SQLite is in WAL mode, so copy a checkpointed snapshot rather than the live
  file:

  ```bash
  fly ssh console
  # in the container:
  python3 -c "import sqlite3; sqlite3.connect('/data/sales.db').execute(\"VACUUM INTO '/data/backup-sales.db'\")"
  exit
  fly ssh sftp get /data/backup-sales.db ./backup-sales.db
  fly ssh sftp get /data/settings/seller.yaml ./seller.yaml      # and the other files under /data/settings
  ```

  Then delete `/data/backup-sales.db` in the container. The `sqlite3` command-line tool is not in
  the image; `VACUUM INTO` from Python is the equivalent of its `.backup`. To make it routine, run
  the same two commands from a scheduled job on your own machine (cron, launchd).

- Fly volume snapshots: Fly takes daily snapshots of every volume and keeps them for a few days;
  `fly volumes snapshots list <volume id>` shows them and `fly volumes create --snapshot-id`
  restores one into a new volume.

Keep backups where you keep customer data: they hold every transcript.

## Upgrading

```bash
cd salescoach
git pull
fly deploy
```

The image is rebuilt from the checkout and the machine restarted. Database migrations run on the
first connection and are transactional; settings on the volume are never touched by an upgrade.
`/health` reports the new `version` once it is up.

## Rotating the password

```bash
fly secrets set SALESCOACH_PASSWORD='a new passphrase'      # or SALESCOACH_PASSWORD_HASH=...
```

Fly restarts the machine with the new value. Session cookies are signed with a key derived from the
password, so every browser is logged out at once and has to log in with the new one. (If you set
`SALESCOACH_SESSION_SECRET` explicitly, change it too to get that effect.)

## Threat model, plainly

- **One password, no user name, no MFA, no lockout beyond the rate limit.** Anyone with the
  password has everything: calls, deals, drafts, the settings. Use a long passphrase, keep it in a
  password manager, and treat the URL itself as private: an unlisted `*.fly.dev` name is not a
  secret, but there is no reason to publish it.
- **The rate limit is per address, in memory.** Five failures per address per fifteen minutes; a
  restart forgets the counts. It slows guessing; it does not make a short password safe.
- **The session cookie** is signed, HttpOnly and Secure, so a script on another site cannot read it
  and it never travels over plain http. It is not bound to a browser: someone who copies it out of
  your browser has your session until it expires or the password changes.
- **The webhook secret opens one door**: `POST /import/webhook` imports a transcript. A leaked
  secret lets someone add calls (and make the model read what they wrote); it reads nothing, sends
  nothing, and touches no other route. Rotate it in Settings if in doubt.
- **The same-origin guard still applies**: every state-changing request must come from the app's
  own pages at the public URL, so another website open in the seller's browser cannot press Send
  or Confirm.
- **The model key and the data** leave the container only towards the provider you chose. Nothing
  is sent to anyone else; nothing is emailed (Gmail is not connected hosted).
- **The platform sees everything**: the volume is not encrypted by the app. Fly, Railway and Render
  encrypt volumes at rest by their own account; read their terms as you would for any customer data.
- **Not covered**: multiple users, roles, audit of who logged in, SSO. This is one seller's tool
  put on a server for one engagement.
