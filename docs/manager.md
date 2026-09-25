# Managers: reviewing a team's work

A cloud install (docs/deploy-cloud.md) has reps, managers and admins. This page says what a manager
sees and can do, what a manager can never do, and what that means for a rep's privacy. It describes
Phase 7 of the multi-user work; the code is `salescoach/manager/` and `salescoach/plugins/manager.py`.

## Who is a manager

A manager is anyone listed as a manager of a team under **Admin, Teams** (the role `manager` on its
own grants nothing). A manager reads the work of every member of every team they manage, and nobody
else's. Taking someone off a team's managers ends their access on the next page they open; there is
nothing cached to expire.

An admin runs the org (people, teams, invites, settings) and reads no one's calls, deals or coaching.
An admin who should review a team is listed as its manager, like anyone else. An admin who manages no
team gets no Team page; the Admin page links to team setup.

A local (single-user, SQLite) install has no teams: `/team` answers "team features need the cloud
install". Comments still work there, on your own calls.

## What a manager sees

**Team** (`/team`, in the navigation only for someone who manages a team). One row per rep:

| Column | What is counted |
|---|---|
| Calls, 7 d and 28 d | calls whose start date is within the last 7 or 28 days, today included |
| To review | calls waiting for the rep's review (awaiting review, no pipeline error) |
| Overdue loops | loops open or waiting, not rejected, with a due date before today |
| Open deals, median health | active deals; the median deal-health score over those that have one (with n) |
| Most often unknown | the active methodology's elements that are neither known nor partial on the most open deals (never assessed counts as unknown), top 3, as "unknown on N of M open deals" |
| Talk share | from live captures and stereo imports only (where the coach could tell who spoke): the mean of the latest 3 calls, and whether it went up or down from the 3 before |
| Follow-ups | calls in the last 28 days with a follow-up email sent, and with one drafted |
| Last activity | the latest call start or email sent |

Every number is counted from the store in SQL and Python; nothing on the page is estimated by a model.
Every number is a link to the list it counts (the Calls, Loops or Deals page filtered to that rep), and
the list uses the same condition, so the number and the list always agree.

Below the table, **patterns across the team**: for each selling pattern the coach tracks (the
"How you sell" family of the Learning page), how many of the team's reps have it active, and how many
of those have it established. Counts only; a pattern appears once at least 3 reps have it. It is
computed from each rep's own Learning page when the Team page is opened and stored nowhere, so it can
never be fed to a prompt. A rep's email voice is never compared or rolled up.

**Calls** (`/calls`, for everyone). Every call the viewer may read, newest first, filtered by rep, date
range, deal, state (awaiting review, reviewed, done, failed, processing, needs the rep), a methodology
gap on the call's deal, or whether a follow-up was drafted or sent. A rep sees their own calls; a
manager sees their own and their team's. The database decides which calls those are: the query adds
no owner condition of its own unless a rep is chosen in the filter.

**A rep's pages, read only.** From the Team page or the Calls list a manager opens a rep's call page,
deal page (with the deal intelligence, stage and outcome, prep briefs), nudges, Loops and Deals lists,
Coach page and Learning page. Each shows everything the rep sees, with a line saying whose it is and
that it is read only. Every button and form that would change something is left out: confirming or
rejecting loops, editing, saving, sending, skipping or redrafting an email, mapping speakers, linking a
deal, the stage editor, pattern verdicts, proposals, a prep brief request, retries and re-runs.

## What a manager can do

- **Comment.** On a rep's call, a comment box under every turn of the transcript ("comment on this
  moment") and a general thread; a thread on a deal and on an email (the follow-up on the call page, a
  nudge on its page). The rep and every manager of the rep's team read the same thread.
- **Leave a coaching note** on a rep's Learning page. The rep sees it on their own Learning page.

That is all. A comment or a note is owned by the rep whose work it is about (it lives with that work)
and written by its author. The rep resolves it when done; its author may also resolve it, and only its
author may delete it. Nobody edits someone else's words.

## What a manager can never do

- Change anything of a rep's: a manager's request to any of the rep's buttons answers 403 and changes
  nothing (tests/isolation/test_manager_review.py requests every write route of the app, as a manager,
  on a rep's objects, and checks every table afterwards).
- Send, save or skip a rep's email, or queue work on the rep's behalf (a redraft, a retry, a strategy
  run, a prep brief, a follow-up nudge). Sending is also refused inside the send path itself.
- Read another team's work, or their own team's once they are no longer its manager.
- Put anything into the coach's prompts. Comments, coaching notes and the team roll-up are read by
  people only; no agent ever sees them (a test fails the build if a prompt-building module reads them).

The database enforces this, not only the pages: every rep-owned table has row-level security that lets
a manager read their team's rows and write none of them (docs/architecture.md, "Isolation").

## For reps: privacy

**Your manager can read everything you record here**: every call and its transcript, every draft
(sent or not), your deals, loops, notes the coach made about how you sell, and your Learning page.
That is what the manager role is for. What your manager cannot do is change any of it or send
anything as you.

You can see when they look. Your call and deal pages show a "Viewed by" line: who opened them and
when (a visit is recorded at most once every ten minutes). The record cannot be edited or deleted by
anyone through the app.

New comments from your manager show on Today ("2 comments from ..."), with a link to each. Resolve
a comment when you have dealt with it.

Other reps never see your work, your comments or your coaching notes, whatever team they are on.

## Under the hood

- `comments` (OWNED): `owner_id` is the rep whose object it is, `author_id` who wrote it,
  `entity_type` call, deal, email, loop or coaching, `turn_idx` for a moment of a call. Its insert
  policy is the one exception to "the owner writes": the author must be the acting user, the owner must
  be someone the author may read, and it must be the real owner of the commented object
  (`app_entity_owner()`); the reason is written next to the policy in `store/rls.py`.
- `access_log` (SYSTEM, insert-only): viewer, the object's owner, the object, when. Readable by the
  object's owner and their managers.
- SQLite migration 11, Postgres `store/pg/0007_manager.sql`; the policies are in the generated
  `store/pg/rls.sql`.
