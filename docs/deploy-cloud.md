# Deploying Sales Coach for a team (cloud mode)

[deploy.md](deploy.md) covers one seller on one container with SQLite and a password. This page is
the multi-user case: `SALESCOACH_MODE=cloud`, Postgres, Google sign-in for everyone in the
customer's Workspace domain, each rep connecting their own Gmail and Calendar. Nothing here
applies to a laptop install; nothing on a laptop install changes.

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

## Environment

| Variable | Required | What |
|---|---|---|
| `SALESCOACH_MODE` | yes | `cloud` |
| `DATABASE_URL` | yes | the app's `postgresql://` URL (run `salescoach migrate` first and after every upgrade; `DATABASE_MIGRATE_URL` may name an owner role for that) |
| `SALESCOACH_PUBLIC_URL` | yes | `https://coach.example.com`: what people type; also the base of the two OAuth redirect URIs |
| `SALESCOACH_SESSION_SECRET` | yes | a long random string; signs the session cookie. Rotating it logs every browser out. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | yes | the Internal OAuth client the Workspace admin created (checklist below) |
| `GOOGLE_ALLOWED_DOMAINS` | yes | comma list of Workspace domains whose members may sign in; checked against the ID token's `hd` claim on the server, never only hinted |
| `SALESCOACH_BOOTSTRAP_ADMIN` | first start | the one address allowed to sign in before any invite exists; it becomes the first admin, exactly once, then is an ordinary user |
| `SALESCOACH_TOKEN_KEYS` | yes | `kid:base64,...`: the key ring for OAuth tokens. Make an entry with `salescoach tokens new-key`. First key encrypts, any listed key decrypts. |
| `SALESCOACH_DATA`, `SALESCOACH_RUNTIME` | yes | as in deploy.md (settings, imported transcripts) |
| the model provider key | yes | as in deploy.md |

`salescoach serve` refuses to start in cloud mode while any of the session secret, the Google
client, the allowed domains or the token keys is missing, and names what is missing.

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
# deploy with both keys listed, then:
salescoach tokens rotate             # re-encrypts every grant under the newest key
# then drop the old key from SALESCOACH_TOKEN_KEYS and deploy again
```

A grant whose key is no longer in the ring cannot be read; `rotate` names such rows and the person
reconnects. The ring is never written to the database or to any file by the app.

## Sessions, offboarding, what a disabled user loses

- A person logs out of one browser (Log out) or all of them (profile page, "Log out everywhere").
- An admin can log a person out everywhere, or **disable** them. Disabling revokes every session
  and every Google grant at once (at Google too, best effort) and refuses their next sign-in. Their
  calls, deals, drafts and coaching stay in the database, owned by them, readable by their team's
  managers as before; nothing is sent or polled as them any more. Enabling them again lets them sign
  in; they reconnect Google themselves.
- Rotating `SALESCOACH_SESSION_SECRET` ends every session on the next request.
- Housekeeping: `sessions.purge()` drops sessions expired or revoked more than a week ago.

## Roles

| Role | Can |
|---|---|
| rep | their own work: calls, deals, drafts, sending from their own mailbox |
| manager | the above, plus reading (never editing, never sending) the work of every team they are listed on |
| admin | the Admin page (people, teams, invites) and Settings. Being an admin grants no access to anyone's calls: an admin reads a team's work only when listed as one of its managers. |
