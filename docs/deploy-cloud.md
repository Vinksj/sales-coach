# Deploying Sales Coach for a team (cloud mode)

[deploy.md](deploy.md) covers one seller on one container with SQLite and a password. This page is
the multi-user case: `SALESCOACH_MODE=cloud`, Postgres, Google sign-in for everyone in the
customer's Workspace domain, each rep connecting their own Gmail and Calendar, and the app split into
web, worker and scheduler processes from the same image. Nothing here applies to a laptop install;
nothing on a laptop install changes.

## What cloud mode changes

- Sign-in is Google only. There is no password; `/login` offers "Sign in with Google". Only
  addresses an admin has put on the list (Admin page) can sign in, and only from the Workspace
  domains in `GOOGLE_ALLOWED_DOMAINS`. The first admin is named in `SALESCOACH_BOOTSTRAP_ADMIN`.
- Sessions live in the database. The browser holds a signed random id; role and status are read
  from the `users` row on every request, so disabling someone or "log out everywhere" takes effect
  at their next request. A session lasts thirty days from its last use.
- Gmail and Calendar are per person. Each rep connects their own from their profile page (You).
  Tokens are AES-256-GCM encrypted at rest under `SALESCOACH_TOKEN_KEYS`; the app never sees a
  refresh token outside the code that hands it to Google, never logs one, never renders one.
- Auto-send is off, whatever `config/automation.yaml` says, and a manager can never send: the
  policy layer refuses anyone but the email's owner in their own session.
- The machine-local features stay off, as in any hosted install (live capture, local transcription,
  the Claude CLI connectors, the Jarvis bridge).
- Settings and raw payloads live in the database (`org_settings`, `raw_payloads`), so no process
  needs a shared volume; see "Org settings and raw payloads".
- The app runs as three kinds of process (web, worker, scheduler); see "Processes".
- Every statement the app runs goes through Postgres row-level security as the app role; see
  "Isolation" in [architecture.md](architecture.md). The policies are `store/pg/rls.sql`, which
  `salescoach migrate` re-applies whenever a build changes them; every process refuses to start until
  it has.
- Call recorders are per person too. Each rep connects their own recorder account (Fathom,
  Fireflies, tl;dv, Granola) with their own API key on their profile page; a call belongs to the rep
  whose recorder delivered it. The admin only chooses which recorders are allowed (Settings > "Where
  calls come from"). See "Cloud" in [sources.md](sources.md).
- Settings (the org's company, models, sources, method, budgets) are an admin's: in cloud mode
  `/setup` answers 403 to anyone else, and the database refuses anyone else's save. A rep's own half
  is their profile page (You).

## Environment

| Variable | Role | Required | What |
|---|---|---|---|
| `SALESCOACH_MODE` | all three | yes | `cloud`: no implicit user, org settings and raw payloads in the database |
| `SALESCOACH_ROLE` | all three | yes | `web`, `worker` or `scheduler` (`all` = one process; see "Processes") |
| `DATABASE_URL` | all three | yes | the APP role's `postgresql://` URL (`salescoach_app`: no superuser, no `BYPASSRLS`) |
| `DATABASE_MIGRATE_URL` | `salescoach migrate`, `salescoach tokens rotate` | yes | the OWNER role's URL (owns the schema, `BYPASSRLS`): DDL, the row-level policies, key rotation. Never given to a serving process |
| `SALESCOACH_PUBLIC_URL` | web | yes | `https://coach.example.com`: what people type; also the base of the two OAuth redirect URIs |
| `SALESCOACH_SESSION_SECRET` | web | yes | a long random string; signs the session cookie. Rotating it logs every browser out. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | all three | yes | the Internal OAuth client the Workspace admin created (checklist below); a worker refreshes the reps' grants with it |
| `GOOGLE_ALLOWED_DOMAINS` | all three | yes | comma list of Workspace domains whose members may sign in; checked against the ID token's `hd` claim on the server, never only hinted |
| `SALESCOACH_BOOTSTRAP_ADMIN` | web | first start | the one address allowed to sign in before any invite exists; it becomes the first admin, exactly once, then is an ordinary user |
| `SALESCOACH_TOKEN_KEYS` | all three | yes | `kid:base64,...`: the key ring for OAuth tokens. Make an entry with `salescoach tokens new-key`. First key encrypts, any listed key decrypts. |
| `WORKER_CONCURRENCY` | worker | no | loops per worker process (default 2) |
| `LLM_BUDGET_USER_USD_DAY`, `LLM_BUDGET_ORG_USD_DAY` | worker (and web, for the Usage panel) | no | daily model-spend caps; see "Model budgets" |
| `SALESCOACH_PG_POOL_SIZE` | all three | no | connections per process (default 16); a worker needs concurrency + 2 |
| `SALESCOACH_DATA`, `SALESCOACH_RUNTIME` | all three | yes | as in deploy.md (imported transcripts, runtime files) |
| the model provider key | worker (and web) | yes | as in deploy.md |

`salescoach serve` refuses to start in cloud mode while any of the Google client, the allowed domains
or the token keys is missing (and, for `web` and `all`, the session secret), and names what is missing.
Run `salescoach migrate` with `DATABASE_MIGRATE_URL` before the first deploy and after every upgrade:
`stores.sales()` refuses to serve a schema at the wrong version, or whose row-level policies are not
this build's, on every role.

## Workspace admin checklist (the customer does this once)

1. Create a Google Cloud project inside the organisation; enable the Gmail API and the Calendar API.
2. Google Auth Platform: Audience = **Internal** (no verification, no CASA, no 100-user cap, no
   seven-day refresh-token expiry; only members of the org can sign in). Branding: app name, support
   email, authorised domain.
3. Data access (scopes): `openid`, `email`, `profile`, `https://www.googleapis.com/auth/gmail.compose`,
   `https://www.googleapis.com/auth/gmail.readonly`, `https://www.googleapis.com/auth/calendar.readonly`.
   Nothing wider: not `gmail.send` on top of `gmail.compose` (compose covers sending and Drafts),
   never `gmail.modify` or `mail.google.com`.
4. Clients: a **Web application** with two redirect URIs:
   `https://<app>/auth/callback` and `https://<app>/auth/connect/callback`.
5. Admin console > Security > API controls > Manage third-party app access: tick "Trust internal
   apps", or add this client id as Trusted with Gmail and Calendar access.
6. Manage Google Services: Gmail and Calendar must not be "Restricted" for the target OU (or step 5
   covers it). If Gmail is Restricted, an unlisted app cannot use its scopes.
7. Tell reps: a Google password change silently unlinks Gmail; they re-link from their profile page.
8. Call recorders: **each rep needs their own recorder account with API access** (Fathom: any plan;
   Fireflies: any plan, Free is limited to 50 API requests a day; tl;dv: Pro or above; Granola:
   Business or Enterprise). There is no org-wide recorder key. Gong, Otter, Avoma and Chorus are not
   supported (org-level APIs only). Confirm with the recorder vendor that storing transcripts in this
   app is allowed under their terms.

## How sign-in works

1. `/auth/google` remembers `state`, `nonce` and a PKCE verifier on the server (ten minutes, one
   use) and sends the browser to Google with scopes `openid email profile`, an `hd` hint and the
   S256 challenge.
2. `/auth/callback` accepts the `state` once, exchanges the code (with the verifier) for tokens,
   parses the ID token with Authlib (signature against Google's JWKS, `iss`, `aud`, `exp`, our
   `nonce`) and verifies it a second time with google-auth. Then, by hand: `email_verified` is true,
   `hd` is in `GOOGLE_ALLOWED_DOMAINS`, the address is in that domain.
3. The address must be on the list: an invited or active user, or `SALESCOACH_BOOTSTRAP_ADMIN` when
   no such user exists yet (created once, under a lock). Anyone else sees "ask your admin for an
   invite". A disabled user is refused.
4. A session row is created and its signed id set as the cookie (HttpOnly, SameSite=Lax, Secure).
   Five refused sign-ins in fifteen minutes lock the client address; five for one email lock that
   email.

## How consent works (Gmail, Calendar)

From the profile page, "Connect Gmail" / "Connect Calendar" sends the signed-in person to Google
with that feature's scopes plus `openid email`, `access_type=offline`, `prompt=consent` and
`include_granted_scopes=true`, so a refresh token comes back and ONE grant per person grows as
features are added (Google caps refresh tokens per user per client at 100; the app never mints a
second one). The consenting Google account must be the person's own address, or the connection is
refused. The row keeps the union of granted scopes; Disconnect revokes the grant at Google and
deletes the row.

When Google answers `invalid_grant` to a refresh (the person revoked access, changed their password
with Gmail scopes granted, six months without use, or an admin restricted the app), the grant is
marked "needs reconnect" once, never retried, and the profile page shows a Reconnect button. Sending
and reply polling for that person pause until they reconnect; nothing else is lost.

## Token key rotation

```
salescoach tokens new-key            # prints kid:base64; add it to the FRONT of SALESCOACH_TOKEN_KEYS
# deploy with both keys listed, then, with the owner role (as for `salescoach migrate`):
DATABASE_MIGRATE_URL=postgresql://owner@.../db salescoach tokens rotate
# or through the app role as an active admin:  salescoach tokens rotate --as <admin user id>
# then drop the old key from SALESCOACH_TOKEN_KEYS and deploy again
```

`rotate` also re-encrypts the reps' recorder keys (`source_connections`); those rows are each rep's own,
so only the owner role sees them all (`--as` an admin reaches the admin's own connections only).

`rotate` re-encrypts every person's grant, and the row-level policy on `oauth_tokens` lets only a
grant's owner or an active admin see it, so the command runs as exactly one of the two identities that
can see them all and says so: the owner role (`DATABASE_MIGRATE_URL`, or `--url`), which bypasses row
security as the migrator does, or `--as` an active admin. With neither it refuses and names both; `--as`
a rep is refused. A grant whose key is no longer in the ring cannot be read; `rotate` names such rows
and the person reconnects. The ring is never written to the database or to any file by the app.

## Sessions, offboarding, what a disabled user loses

- A person logs out of one browser (Log out) or all of them (profile page, "Log out everywhere").
- An admin can log a person out everywhere, or **disable** them. Disabling revokes every session
  and every Google grant at once (at Google too, best effort) and refuses their next sign-in. Their
  calls, deals, drafts and coaching stay in the database, owned by them, readable by their team's
  managers as before; nothing is sent or polled as them any more. Enabling them again lets them sign
  in; they reconnect Google themselves.
- Rotating `SALESCOACH_SESSION_SECRET` ends every session on the next request.
- Housekeeping: `sessions.purge()` drops sessions expired or revoked more than a week ago.
- The `sessions` table never holds a session id: its `id` column is sha256 of the id the cookie
  carries, so a backup or a leaked copy of the table cannot be replayed as anyone's session.

## Roles

| Role | Can |
|---|---|
| rep | their own work: calls, deals, drafts, sending from their own mailbox |
| manager | the above, plus reading (never editing, never sending) the work of every team they are listed on |
| admin | the Admin page (people, teams, invites) and Settings. Being an admin grants no access to anyone's calls: an admin reads a team's work only when listed as one of its managers. |

## Processes

One image, three roles, chosen by `salescoach serve --role` or the `SALESCOACH_ROLE` variable the
container's entrypoint passes through:

| Role | Runs | HTTP | Scale |
|---|---|---|---|
| `all` (default) | the web app, one worker loop, every scheduler duty, the embed/learning loops, the heartbeats: today's single process, what a laptop and the one-seller container run | yes | exactly one (SQLite has one writer; on Postgres it would be one of everything) |
| `web` | the web app only. No worker thread, no duties, no Jarvis sync, no embed loop | yes | as many as you like |
| `worker` | `WORKER_CONCURRENCY` (default 2) worker loops, each on its own connection, plus a heartbeat. Nothing else | no | as many as you like |
| `scheduler` | the duties (follow-ups, reply polling, calendar, auto-send, the sources poller, the learning recompute) and the intel embed loop, under a leader election, plus a heartbeat | no | one is enough; two or more elect one leader |

**Leader election.** Every `scheduler` process opens a connection of its own (not from the pool)
and tries `pg_try_advisory_lock(hashtext('salescoach:scheduler:' || current_schema()))`. The holder
starts the duties and refreshes `state['ops:scheduler:leader']` every 15 s; the others write a
heartbeat saying `leader: false` and try again every 5 s. The lock is a session lock: when the holder's
process ends (a deploy, a crash) or its connection drops, Postgres releases it and a standby takes over
on its next try. A leader that finds its lock connection dead stops its duties (a duty in the middle of a
round finishes that round; its thread ends after) and goes back to trying. On SQLite there is no
election: one process per file is assumed, and `salescoach serve` (role `all`) is that process.
Two scheduler processes on SQLite would both run the duties; do not do that.

**Fairness on the bus.** Every workflow event carries a `priority` (interactive requests such as a
redraft, a retry, a strategy, prep or coach-report request publish at 10; new calls, imports and the
daily duties at 0; history backfills at -10) and an `owner`. A
claim takes the highest priority first, then the oldest id, and **skips events whose owner already has
an event running**: on Postgres the claimer takes a session advisory lock on the owner
(`pg_try_advisory_lock(hashtext('salescoach:owner:<id>'))`) before marking the row running and
releases it when the event is settled, so two workers can never both run one owner's events, a rep's
bulk import cannot take every worker while another rep waits, and an owner's events of equal priority
are handled in id order. A deal has one owner, so a deal's events keep their order too (only an
interactive request, priority 10, overtakes a pending import; a methodology switch's re-strategy stays
at 0 for that reason). Pooled connections drop every
advisory lock when they go back to the pool. On SQLite the claim runs under the file's one write lock
and checks running owners with a `NOT EXISTS`, which the write lock makes race-free.

**Heartbeats.** Worker and scheduler processes write a row into the org-wide `state` table every 15 s,
acting for nobody (the row-level policy on `state` lets any connection read and write the `ops:%` keys,
and only those):

| Key | Body |
|---|---|
| `ops:worker:<hostname>:heartbeat` | `{"at", "pid", "host", "started_at", "concurrency", "handled", "busy"}` |
| `ops:scheduler:<hostname>:heartbeat` | `{"at", "pid", "host", "started_at", "leader"}` |
| `ops:scheduler:leader` | `{"host", "pid", "since", "at"}`, refreshed by the holder |

`GET /health` on a web process answers `{"status", "version", "db", "role", "worker", "processes",
"configured"}` where `processes` lists those rows with `age_s` and `stale` (older than 50 s), so a dead
worker or scheduler is visible from outside: the web tier keeps serving, the nav shows "worker off" and
the queue grows. `salescoach health` is the container health check for every role: for `web` and
`all` it fetches `/health` on `PORT`; for a worker or a scheduler it reads this host's own heartbeat row
and exits 1 when it is missing or stale. `salescoach status` prints the queue per owner (pending,
running, failed, the oldest pending event) under the recent calls.

**Deploying the three.** `render.yaml` describes one web service and two background workers from the
one Dockerfile plus a managed Postgres on the private network; each service sets `SALESCOACH_ROLE`
and gets `DATABASE_URL` from the database. `fly.toml` declares process groups `web`, `worker` and
`scheduler` (and `app`, the single-seller role `all` with its volume): `fly scale count app=0 web=1
worker=1 scheduler=1` for a team, `fly scale count worker=3` when the queue is long. Run `salescoach
migrate` (with `DATABASE_MIGRATE_URL`, the owner role) before the first deploy and after every upgrade:
`stores.sales()` refuses to serve a schema at the wrong version, on every role. Deploy the scheduler last
so the leader that takes over runs the new build.

## Model budgets

`LLM_BUDGET_USER_USD_DAY` and `LLM_BUDGET_ORG_USD_DAY` (or `llm.user_usd_day` / `llm.org_usd_day` in
the `budget` settings, which the environment overrides) cap what the models may cost per calendar day,
in USD. Before an agent calls a provider it sums `agent_runs.cost_usd` since the acting user's midnight
(their timezone) for that user and for everyone; past either cap it records a run with `error`
`budget_deferred: ...` and the worker defers the event without spending an attempt, exactly as for a
provider's 429, so the work resumes after the deferral (15 min) or the next day. Costs come from the
usage counts the API returns and the price table in `config/models.yaml` (`prices:` per model id, USD
per million input and output tokens; an unpriced model costs `None` and cannot be budgeted, so keep the
table current for the models you pick). The `claude_code` provider reports the CLI's own figure. Setup
> Model (admins) shows today's spend for the org and per user; `/me/setup` shows the user's own. Row
security shows a rep only their own runs, so on Postgres the org total and the per-user sums come from
two SECURITY DEFINER functions (`app_org_spend_since`, `app_spend_by_owner_since`) that return sums,
never rows: every active user gets the org total (the cap needs it), an admin gets every user's sum,
anyone else only the users they may read.

## Org settings and raw payloads

In cloud mode the settings overlay (what `data/settings/<name>.yaml` is on a laptop: the org profile,
the model choice, the sources, the methodology, the budgets) lives in the `org_settings` table, one row
per name with a `version` that every save bumps. `config.load()` reads the row's version on every call
and caches the merged settings by it, so web, worker and scheduler see a change on their next read with
no restart and no shared file. Secrets (API keys) stay in the environment or `secrets.env`; the style
guide is per user (`users.style`). Nothing in the wizard changes: it saves through the same function.
Any connection may read the table (settings are read before anyone is bound: the scheduler's
intervals, the sign-in page); only an active admin may write it.

What a recorder, an upload, a paste or the webhook delivered is kept as it arrived: on a laptop under
`data/inbox/<kind>/`, in cloud mode in the `raw_payloads` table (owned by the user whose import it was,
one row per distinct payload per owner, written in the import's transaction so a refused import leaves
nothing). The table is OWNED, under the same row-level policies as a call: its owner (and their
managers) read it, nobody else. Nothing in the pipeline reads a payload back; `salescoach payloads
export --out DIR --as <user id>` (or without `--out`, JSON lines on stdout; `--since` to bound it)
writes one user's out for support.
