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
  the Claude CLI connectors, the Jarvis bridge). The calendar is each rep's own Google Calendar,
  read-only, through their own grant (see "Calendar: what is read, and reconnecting").
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
| `DATABASE_MIGRATE_URL` | `salescoach migrate`, `salescoach tokens rotate`, `import-sqlite`, `export --user`, backups | yes | the OWNER role's URL (owns the schema, `BYPASSRLS`): DDL, the row-level policies, key rotation. Never given to a serving process |
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

## Calendar: what is read, and reconnecting

"Connect Calendar" asks for `calendar.readonly` only (plus `openid email`). With it the app reads the
rep's own primary Google Calendar and nothing else; it never creates, changes or answers an event and
has no code path that could (`automation/gcal.py` issues one request, `GET .../calendars/primary/events`):

- **Upcoming meetings.** The scheduler's calendar duty (every `calendar.scan_hours`) and the page's
  "Refresh calendar" read the next `calendar.upcoming_days` for each rep, as that rep: title, start and
  end, the guests' addresses and their responses, status, event type, free/busy flag and the join
  link (`hangoutLink` or a video `conferenceData` entry). Declined, cancelled, all-day, out-of-office,
  working-location and "free" entries are not listed. A meeting with a deal's people gets a prep brief.
  The Calendar page and Today's "Upcoming calls" show the rep's own meetings only; there is no Record
  button in cloud mode (calls come from the rep's recorder).
- **Meeting times in drafts.** "Fill times from calendar" on a draft with `[SLOTS]` reads the rep's
  own calendar for the next business days and proposes two or three free times under the org's
  scheduling rules (Settings), in the rep's own timezone (their profile; an org-wide `timezone` in the
  scheduling rules is only the fallback for a rep who has none). The draft records whose calendar was
  read ("Google Calendar of asha@...").
- **Incremental reads.** After a full read of the window the app keeps Google's sync token in the
  rep's own `user_state` (`automation:calendar:sync_token`, an opaque cursor, not a credential) and
  later asks only for what changed. Google expiring it (410) means one full read; the window is also
  re-read in full at least daily.

A rep who has not connected Calendar simply has no meetings listed: the duty notes "calendar not
connected" for them and moves on (no errors, no retries), and the Calendar and Today pages show a
"Connect your calendar" card. When Google stops accepting the link (`invalid_grant`, or a 401/403
that says the grant lacks the scope), the grant is marked "needs reconnect" once; the pages show
"Reconnect your calendar", the meetings already read stay listed, and slot filling says "reconnect
your calendar" instead of guessing. Reconnecting from the profile page (You > Google > Calendar) is
all it takes; the next read is a full one.

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
- A person leaving for good is **offboarded** (their work handed to another rep, or deleted): see
  "Offboarding and data requests" below. Disabling alone keeps their work where it is.
- Rotating `SALESCOACH_SESSION_SECRET` ends every session on the next request.
- Housekeeping: `sessions.purge()` drops sessions expired or revoked more than a week ago.
- The `sessions` table never holds a session id: its `id` column is sha256 of the id the cookie
  carries, so a backup or a leaked copy of the table cannot be replayed as anyone's session.

## Roles

| Role | Can |
|---|---|
| rep | their own work: calls, deals, drafts, sending from their own mailbox |
| manager | the above, plus reading (never editing, never sending) the work of every team they are listed on, commenting on it, and the Team page ([docs/manager.md](manager.md)) |
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
(`pg_try_advisory_lock(hashtext('salescoach:owner:<schema>:<id>'))`) before marking the row running and
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
one Dockerfile plus a managed Postgres on the private network; each service runs `salescoach serve
--role <role>`, sets `SALESCOACH_ROLE` and takes `DATABASE_URL` (the app role, entered by hand once the
role exists). The web service's pre-deploy command is `salescoach migrate`, with `DATABASE_MIGRATE_URL`
from the database (its owner); the web process unsets that variable before it serves. `fly.toml` declares process groups `web`, `worker` and
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

## Backup and restore

Everything the app knows is in the one Postgres database: calls and transcripts, deals, loops, emails,
coaching, the settings (`org_settings`), what each source delivered (`raw_payloads`), the encrypted
Google grants and recorder keys, sessions. There is no shared volume and nothing in `SALESCOACH_DATA`
that a cloud install needs to keep (a laptop keeps payload files under `data/inbox`; a cloud install keeps
them as rows). What is NOT in the database, and must be kept elsewhere: the environment (above), above all
`SALESCOACH_TOKEN_KEYS` (without the key that encrypted them, the grants and recorder keys in a backup are
unreadable: people reconnect) and `SALESCOACH_SESSION_SECRET` (without it every browser signs in again).
Keep both in the platform's secret store and in the org's password manager.

**Managed backups (Render).** Use a paid Render Postgres plan: it takes daily snapshots and keeps
write-ahead logs for point-in-time recovery (the window depends on the plan; check it before launch and
write it down). Keep the database on the private network (no public connection string) and in the same
region as the services. A point-in-time restore creates a NEW database: point `DATABASE_URL` and
`DATABASE_MIGRATE_URL` at it (the roles come with it) and redeploy.

**A logical backup you hold yourself** (weekly, and before every upgrade), as the OWNER role, because only
it sees every row (the app role's dump would be filtered by row-level security to nothing):

```
pg_dump "$DATABASE_MIGRATE_URL" --format=custom --no-owner --file=salescoach-$(date +%F).dump
```

Store it encrypted (it holds every transcript) where the customer's retention and access rules apply;
delete old dumps on the same schedule as the retention setting, or a deleted call lives on in a dump.

**Restore drill** (do it once before launch, then quarterly; write down the time it took):
1. Create an empty database; create the app role in it as for a first deploy
   (`CREATE ROLE salescoach_app LOGIN PASSWORD '…' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
   GRANT CONNECT ON DATABASE … TO salescoach_app;`).
2. `pg_restore --no-owner --role=<owner role> --dbname="$NEW_MIGRATE_URL" salescoach-YYYY-MM-DD.dump`
3. `DATABASE_MIGRATE_URL=$NEW_MIGRATE_URL salescoach migrate`: it re-applies the row-level policies and
   the app role's grants (rls.sql), and brings an older dump up to this build's schema.
4. Start a `web` process against it with the same `SALESCOACH_TOKEN_KEYS` and session secret; sign in as a
   rep and a manager; open a call; check that Gmail still shows as connected (the key ring decrypts).
5. `salescoach status` and `salescoach migrate --check` both clean. Then throw the drill database away.

## Launch checklist

Environment, per service (see "Environment" for what each is): `SALESCOACH_MODE=cloud`,
`SALESCOACH_ROLE` (web / worker / scheduler), `DATABASE_URL` (app role), `DATABASE_MIGRATE_URL` (owner
role; only for migrate, tokens rotate, import-sqlite, export --user and backups, never a serving process),
`SALESCOACH_PUBLIC_URL`, `SALESCOACH_SESSION_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`GOOGLE_ALLOWED_DOMAINS`, `SALESCOACH_BOOTSTRAP_ADMIN`, `SALESCOACH_TOKEN_KEYS`, the model provider's API
key (with a spend limit set at the provider), `LLM_BUDGET_ORG_USD_DAY`, `LLM_BUDGET_USER_USD_DAY`,
`WORKER_CONCURRENCY`, `SALESCOACH_PG_POOL_SIZE` (optional), `SALESCOACH_DATA`, `SALESCOACH_RUNTIME`.

1. **Database.** Managed Postgres with point-in-time recovery, private network. Create the app role;
   `salescoach migrate` with the owner role; `salescoach migrate --check` exits 0.
2. **Workspace admin checklist** above, done by the customer's Google admin: Internal OAuth client, the
   five scopes and nothing wider, both redirect URIs, trusted app access, Gmail and Calendar not
   Restricted for the reps' OU.
3. **Recorder accounts per rep.** Every rep has their own account on the recorder the team uses, with API
   access on their plan; the vendor's terms allow storing transcripts here. The admin allows that recorder
   (Settings > Where calls come from); each rep connects their own key on their profile page.
4. **First admin.** Deploy with `SALESCOACH_BOOTSTRAP_ADMIN=<their address>`; they sign in first and
   become the admin, once.
5. **Settings.** The admin fills in the org's company, offering and ICP, picks the models and tests them,
   chooses the method (Settings).
6. **Invites, teams and managers.** Admin page: invite every rep and manager by address with their role;
   create the teams, put reps in them, name each team's managers.
7. **Recording consent.** The customer's own wording, in Settings > Review > "Recording consent and
   retention": every rep sees it on each call page, on My meetings and on Today.
8. **Retention.** The customer's period in days, same place (empty keeps everything). `salescoach retention
   --dry-run` says what the first run would delete, per rep.
9. **Budgets.** `LLM_BUDGET_ORG_USD_DAY` and `LLM_BUDGET_USER_USD_DAY` set; a spend limit at the provider too.
10. **An existing laptop install?** Bring it in before anyone else starts, while the org is empty:
    `salescoach import-sqlite ~/.claude/sales-coach/data/sales.db --as <their email> --dry-run`, read the
    counts, then the same without `--dry-run`. It refuses a non-empty org. It copies the database, not the
    payload files under `data/inbox`, the laptop's `state`, settings files or Google tokens: the person
    signs in, reconnects Gmail, Calendar and their recorder, and the admin enters the org settings.
11. **Test sign-ins.** A rep signs in, connects Gmail and Calendar and their recorder, and sees a call of
    theirs arrive; a manager signs in and sees that rep's call read-only on /team and /calls; a second rep
    does NOT see the first rep's call (404).
12. **Backups.** The first `pg_dump` taken and the restore drill done once (above).
13. **Security review sign-off.** The independent review of this build (the plan's gate before any real
    user is invited) is done, its findings fixed or accepted in writing by the customer. No invite goes out
    before this line is ticked.

## Offboarding and data requests

**A rep leaves.** Admin page > the person's row > Offboard. Choose one, then type their email to confirm:
- **Hand their work to** another active rep: every call, deal, loop, email and draft (with the comments on
  them, whose authors do not change) becomes that rep's, in one transaction. What describes the person,
  not the work, is deleted rather than handed over: their learned patterns and proposals, pattern
  observations, seller patterns and observations, coach reports, live-coach nudges, calendar cache and
  meetings, recorder connections, the coaching notes written about them, their bookkeeping and speaker
  labels. The receiver's managers now read the work; the leaver's managers read it only if they also
  manage the receiver. The moved calls still show the leaver as the one speaking ("me"): they are history,
  handed over.
- **Delete all of their work**: every row they own goes. Deals, accounts and people in the shared directory
  stay (other reps may use them).
Either way they are disabled, signed out everywhere and their Google grants revoked (at Google too, best
effort), and the Admin audit (events: `admin.user.offboard` with the per-table counts, then
`admin.user.disable`) records who did it. Neither can be undone except from a backup. Just disabling
someone (Disable) keeps their work where it is; retention does not reach a disabled user's calls, so
offboard people who have left.

**A data request (access / portability).** A person downloads everything that is theirs from their
profile page ("Download my data", `/me/export`): a zip with one JSON file per table and a README.json.
For someone who can no longer sign in, an operator runs `salescoach export --user <email> --out file.zip`
with the owner role (`DATABASE_MIGRATE_URL`); it selects the same rows (owner_id = that user) and never
includes secrets (session keys, OAuth tokens, recorder keys).

**A deletion request (erasure).** Offboard with "Delete all of their work". Names and addresses that other
reps' deals also use stay in the shared directory; remove or edit those people by hand if the request
covers them. Then remember the backups: dumps and the managed PITR window keep the data until they age out.

**Retention.** Settings > Review > "Keep calls for (days)". The scheduler's retention duty runs daily (in the
scheduler's leader), per rep, in that rep's own session: it deletes calls older than the period with their
transcripts, analyses, agent runs, closed loops, unsent drafts, the replies to those drafts, comments and
views, and the raw payload, and writes a `retention.purge` event per batch. Open loops and deals stay.
`salescoach retention --dry-run` counts; `salescoach retention` runs a round now.


## End-to-end check

`e2e/run.sh` runs a whole team install on your machine, in Docker, with no outside service involved:
the real image as `web`, `worker` (`WORKER_CONCURRENCY=2`) and two `scheduler` replicas, Postgres 16, a
one-shot `migrate` (creates the app role, runs `salescoach migrate` as the owner role on the org database
and on a second, empty one), and `fakes`, one small process standing in for Google (OIDC sign-in and
consent with PKCE, RS256 ID tokens and their JWKS and x509 certificates, Gmail, Calendar), Fireflies,
Fathom and an OpenAI-compatible model that answers every agent's schema.

```
e2e/run.sh                      # build the image, start the stack, run e2e/test_e2e.py, tear everything down
E2E_KEEP=1 e2e/run.sh           # leave the stack up afterwards; `e2e/run.sh down` removes it
PYTHON=/path/to/venv/bin/python e2e/run.sh -k budget     # a Python with the project installed; pytest args pass through
```

The tests walk the Verification list of the plan, in order, as the people would: the bootstrap admin signs
in and sets the org up (profile, the model); invites two reps, a manager and a rep with no recorder, and makes
a team; everyone signs in through the fake Google; both reps connect their own Fireflies account (different
keys, the same meeting in both) and press Import now, and each gets their own copy, analysed by the worker to
a drafted follow-up; rep B gets 404 for every page and button of rep A's; the manager sees both on /team and
/calls, A's call then shows "Viewed by", the manager's comment on a turn shows on A's page, and the manager's
Send on A's email is refused (403) with nothing reaching Gmail; a 1 USD per-user daily cap defers B's next job
(no attempt spent, nothing failed) while A's runs; A connects Gmail and sends: the fake Gmail recorded exactly
one message, from A's grant, with the draft's To, Message-ID and send key; exactly one scheduler holds the
lock, and `docker kill` of it hands the lead to the other within the 5 s retry; offboarding B to A ends B's
session at once and moves B's call to A, which the manager still reads; `salescoach import-sqlite` of a
synthetic laptop database (built with the test fixtures) into the second database round-trips every count;
and the rep with no recorder connects Fathom and gets their own call from it. Any traceback in the web, worker
or scheduler logs other than the deliberate budget deferral fails the run.
`/health` and `salescoach health` are checked on every process.

Ports, on 127.0.0.1 only: 18140 (the app; `SALESCOACH_PUBLIC_URL`), 19000 (the fakes: the test's "browser"
visits the fake Google there) and 15432 (Postgres, read by the test as the owner role). The test only reads
the database, except for two operator acts that have no page: the daily budget (written into `org_settings`,
as Settings would) and `import-sqlite` (run in the web container). A failed run leaves every service's log
in `e2e/logs/` (not tracked); whatever happens, the containers, volumes, network and image are removed.

**How the app reaches the fakes.** Only through settings the app already has (the model provider's base URL,
set from Setup like any OpenAI-compatible endpoint) and four endpoint overrides in `salescoach/endpoints.py`:
`GOOGLE_OAUTH_BASE` (sign-in, token, revoke, signing keys), `GOOGLE_API_BASE` (Gmail and Calendar),
`SALESCOACH_FIREFLIES_URL` and `SALESCOACH_FATHOM_BASE`. Unset, every URL is the vendor's. They are honoured
only together with `SALESCOACH_E2E=1`: set without it, `salescoach serve` refuses to start and any call that
would use one raises, so a stray variable cannot send a Google code, a token or a rep's recorder key anywhere
else. Nothing that is checked changes: the fake's ID tokens go through the same verification as Google's
(signature against the keys at the overridden URL, issuer, audience, expiry, nonce, `hd`, the allow-list).
Never set any of these five variables in a real deployment.
