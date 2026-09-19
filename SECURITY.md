# Security

## Threat model

Sales Coach is a single-user application that runs on the seller's own computer and listens on
`127.0.0.1` only. There are no accounts and no authentication: anyone who can reach the port can
read the data and press Send, so **the port must never be exposed** to a network or the internet.
Within that model the app defends against three things. First, other web pages in the same
browser: every request must carry a `Host` of `127.0.0.1` or `localhost` (which stops DNS
rebinding), and every state-changing request must carry an `Origin` or `Referer` whose scheme,
host and port are exactly the server's own, so a page on another site, or on another localhost
port, cannot submit a form. Second, hostile content: transcripts, emails and recorder payloads
are treated as data; model output is validated against the cited transcript turns, recipients
are restricted to people on the call or the deal, agents run without tools, the calendar
connector is read-only with write tools explicitly disallowed, and no email leaves without an
explicit click. Third, leaking secrets: API keys and the webhook secret live in
`data/settings/secrets.env` (mode 0600, written atomically), are write-only in the UI, are read at
call time into request headers only, and are scrubbed from error text and logs. Files on disk
(the SQLite database, recordings, settings) are **not encrypted**; protect them with your
operating system account and full-disk encryption.

### The webhook exemption

`POST /import/webhook` is the one route meant to be called by something other than the app's own
pages. It is exempt from the Host and Origin checks only when the method is POST, the path is
exactly `/import/webhook`, and the `X-Salescoach-Secret` header matches the configured secret
under a constant-time comparison. With no secret configured the webhook is off (403). The secret
opens no other route. The Host check is skipped for an authenticated webhook call because a
tunnel presents its own host name; everything else arriving through a tunnel is refused. The
payload cannot choose a deal, mark a call as history or trigger anything beyond a normal
import, and bodies over 5 MB are refused. Create or rotate the secret with
`salescoach sources webhook-secret` or in Setup; it is shown once.

### The hosted install

[docs/deploy.md](docs/deploy.md) describes the one configuration that is meant to be reached over
the internet: a container behind the platform's https proxy with `SALESCOACH_PASSWORD` and
`SALESCOACH_PUBLIC_URL` set. Then every request except `/login`, `/logout`, `/static/*`, `/health`
and the webhook needs a signed, HttpOnly, Secure session cookie obtained with the password
(constant-time comparison, five failures per address per fifteen minutes); the `Host` and `Origin`
checks compare against the public URL instead of localhost; forwarding headers from the trusted
proxy are accepted; and the webhook counts every request as remote, so it works only with
"Allow requests through a tunnel" on. It is still one password for one seller: no user accounts,
no roles, no MFA. Without a password, `serve` refuses to bind to anything but a loopback address.

### Known limits

- On a laptop, no authentication and no TLS: localhost only, by design. Hosted, one password and
  the platform's TLS; nothing finer.
- Data at rest is not encrypted by the app.
- Transcripts, deal context and drafts are sent to the model provider you configure.
- `serve --host` accepts another bind address only with a password set (or
  `--allow-unauthenticated`); without `SALESCOACH_PUBLIC_URL`, requests whose `Host` is not
  `127.0.0.1` or `localhost` are still refused. Do not put the localhost install behind a reverse
  proxy; use the hosted configuration instead.
- The Fireflies and Fathom adapters are untested against the live services.

## Supported versions

Only the latest commit on the default branch. There are no maintained release branches yet.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

> **PLACEHOLDER, to be replaced before the first public release:**
> report privately to `security@REPLACE-ME.invalid`, or through the repository host's private
> vulnerability reporting once the repository URL is decided.

Include what you found, how to reproduce it, and what an attacker could do with it. Things of
particular interest: any way to send an email or close a loop without a user action, any bypass of
the Host or Origin checks, any way content from a transcript or an email is followed as an
instruction, any place a secret reaches a log, a page or an error, and any path or command
injection through an import. Please give the maintainers a reasonable time to fix the problem
before you publish.
