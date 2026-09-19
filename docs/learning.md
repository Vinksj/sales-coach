# The learning layer

The coach keeps a store of patterns it has counted: how you sell, how you edit drafts, which
follow-ups get replies, which live nudges you act on, which buyer roles and objections turn up.
Some of these go back into prompts. This page says what is counted, when a pattern becomes
something the coach acts on, what reaches a prompt, and how you correct it.

Three rules hold everywhere (`salescoach/learning/`, thresholds in `config/learning.yaml`):

- **No model is called in this package.** Observations are taken from facts already in the store
  and counted in code. Every number you see is a count made in code, shown with its `n`. Labels,
  never percentages, except one reply rate that appears only at `n >= 20`.
- **What reaches a prompt is a rule, a tag name and a coarse count. Never text from a call or an
  email.**
- **You outrank it.** Your Confirm, Wrong, Retire, Merge and "do not use in prompts" are written
  through the memory gate as user input. A recompute reads them and never overwrites them.

## Outcomes first

Without outcomes the system would learn from a model's opinion, so ground truth comes first.

**What you record** (deal page): stage, status (active, paused, won, lost), value, currency,
close target, lost reason. Marking a deal won or lost needs an explicit confirm; lost needs a
reason from the list in `learning.yaml` ("Other" needs a few words). Each stage or status change
adds a row to the deal's stage history.

**What is derived**, by code only, from rows already stored:

| Outcome | Means |
|---|---|
| `email_replied` | a buyer reply in the email's thread within 5 business days |
| `meeting_after_email` | a calendar meeting on the same deal first seen within 10 days after the email went out |
| `loop_closed_on_time` | the loop was done on or before its due date |
| `call_advanced` | facts only, against the previous call on the same deal: a new buyer-side participant or stakeholder, a buyer-owned loop closed on time, or a methodology element that went from unknown to known or partial. Never a model's verdict. The first strategist run after a methodology switch does not count |

A derived outcome is 1 (happened), 0 (did not) or empty while its window is still open.

Until 10 deals are closed, the Learning page says so: everything shown is co-occurrence with its
`n`, and no causal claim is made.

## What is observed

| Family | One observation is | Source |
|---|---|---|
| `seller` (how you sell) | a selling-behaviour tag on a call (`config/seller_taxonomy.yaml`, or a `new:` tag) | what the call analyst reported seeing on that one call, with the turns it cites |
| `seller_series` | talk share, questions asked, discovery slots filled, per call | the final live-coach state; live captures and stereo imports only. Shown as values with `n`, no judgement |
| `email_voice` | at most one style rule per sent email you edited | the difference between the draft and what you sent, by fixed heuristics |
| `followup` | a sent nudge: its sequence number, weekday, days since the last touch, recipient role | outcome = `email_replied` |
| `nudge_trigger` | a live nudge that was shown: followed, ignored, dismissed or unrated | the live coach's log. Replays are stored flagged and never counted |
| `persona` | a stakeholder's title mapped to a bucket (finance, procurement, ...), per deal | the deal's stakeholders; buckets in `learning.yaml` |
| `objection` | an objection type on a call | the live coach and the analyst's claims |

An observation is evidence by reference only: ids, turn numbers and counts. It does not count
when it is low confidence, comes from a replay, or belongs to a pattern you marked wrong. Counts
are distinct calls and distinct deals, never raw observations.

### Email voice rules

The only rules that can exist, first match wins: a listed filler phrase removed
(`remove_phrase:<id>`), the sign-off changed to another listed sign-off, the greeting changed or
dropped, a stock closing line dropped, exclamation marks removed, shortened (final at or below
0.80 of the draft's words) or lengthened (at or above 1.25). Drafts under 25 words say nothing
about length.

The phrase, sign-off and closing-line lists are fixed in `config/learning.yaml`. A rule can carry
only a phrase from those lists, never a sentence, a name or a number from the email, because
either version may quote a customer.

## Promotion thresholds

The window is the last 10 analysed calls (`promotion.window_calls`).

| Label or status | Rule (`seller` and `objection` families) |
|---|---|
| emerging | seen on at least 3 calls and 2 deals, all time, and on at least 30% of the calls in the window |
| established | seen on at least 6 calls over at least 3 deals, and in both the newer and the older half of the window |
| dormant | not seen in the last 10 analysed calls |
| retired | not seen in the last 20 analysed calls, or retired by you |
| returned | a dormant or absence-retired pattern that recurs is revived and flagged "returned". One you retired or marked wrong is never revived |

A pattern is **candidate** until it qualifies, then **active**. Per family:

| Family | Active when |
|---|---|
| `seller` | it has a label (emerging or established) |
| `email_voice` | 3 supporting edits, or you confirm it. It never goes dormant: no further edits is what a working rule looks like |
| `followup` | 20 decided sends in the bucket. Below that the page shows raw counts only, no rate. **Never fed to a prompt at any n** |
| `nudge_trigger` | shown 15 times live. Then, if ignored plus dismissed is at least 60% of shown, the coach **proposes** lowering that trigger's weight (current x 0.8, never below 0.3). It never edits the config itself |
| `persona`, `objection` | observation-only until the family has observations on at least 8 deals and the bucket on at least 3 deals |

Your Confirm promotes a candidate to active at once.

### Proposals

Two kinds, both only ever proposals on the Learning page:

- **Merge**: a `new:` tag whose words are close (token-set similarity 0.5 or more) to a taxonomy
  tag or another new tag. Nothing is merged automatically.
- **Trigger weight**: as above. Accepting writes the weight to your own
  `data/settings/live_coach.yaml`.

At most one open proposal per subject. A decided proposal stays as history, and the same subject
is proposed again only when the evidence has grown to twice the `n` at your decision and the
condition still holds.

## What reaches prompts

Only patterns that are **active**, not marked wrong, not retired, not merged away and not flagged
"do not use in prompts". At most 3 per prompt (`feedback.max_patterns`; values above 3 are
ignored). Your confirmed patterns come first, then established before emerging.

| Prompt | Receives | Config flag |
|---|---|---|
| Prep writer | your top learned weaknesses, then one strength (`seller`) | `feedback.prep` |
| Follow-up email drafter | email-voice rules, as imperatives | `feedback.email_drafter` |
| Nudge email drafter | the same rules | `feedback.nudge_drafter` |
| Deal strategist | persona and objection patterns, labelled as priors, and only for buckets seen on **that deal** | `feedback.strategist` |
| Live coach | one priority habit, plus a bounded boost (0.1 by default, never more than 0.3, never above a weight of 1.0) to the one trigger that counters it. Applied after the methodology's weights and before your own `live_coach.yaml`, so a weight you set always wins | `feedback.live_coach` |

Each fed pattern carries its id, and the ids a run saw are recorded with the run
(`agent_runs.input_refs.patterns`). The Learning page shows "Used in" per pattern: which prompts
it feeds now and how many runs saw it.

Counts are shown to a model as coarse buckets ("seen on 6+ calls", steps 1, 2, 3, 6, 10, 20, 50,
100), with no timestamps. The rendered prompt is the cache key for an agent step, so a prompt
must change when a belief changes and not when a counter ticks.

Set any `feedback.<target>` to `false` in your `learning.yaml` to stop feeding that prompt. A
failure while reading patterns feeds nothing; it never fails the step that asked.

## Correcting the coach

On `/learning` ("what the coach believes"), every candidate, active and dormant pattern is
listed by family with links to its evidence:

- **Confirm**: it is true. Promotes a candidate; sorts first among what prompts receive.
- **Wrong**: it is not true. The pattern is retired, its observations are excluded from every
  count, and observations that arrive later are excluded too. It is never revived.
- **Retire**: it was true and no longer matters. Never revived by recurrence.
- **Merge into**: fold one pattern into another of the same family. Only you merge.
- **Do not use in prompts**: keep counting it and showing it, but never feed it to a prompt. It
  says nothing about whether the pattern is true.
- Each of these can be undone.
- **Accept / Dismiss** on a proposal.

Editing a draft before you send it is also a correction: the difference between the draft and
what you sent is what the email-voice family learns from.

## What is never learned, and what never crosses deals

- No text from a transcript or an email is stored as a pattern or fed to a prompt. Evidence is
  ids, turn numbers and counts.
- Persona and objection priors reach the strategist only for buckets already observed on the deal
  being assessed. A pattern about CFOs is not injected into a deal that has no finance
  stakeholder.
- The raw edits the email drafter sees as examples are limited to the same deal's emails.
- Follow-up effectiveness is reported, never fed back.
- Replays of finished calls never count.
- Nothing is promoted from a single deal: every label needs at least 2 deals, and persona and
  objection families need 8.
- Nothing is sent anywhere. The weekly digest (`salescoach learn --digest`, also on the page)
  is built from pattern summaries, proposal summaries, deal names and your own lost reasons. It
  is never emailed or scheduled.

## When it runs

A recompute (outcomes, then patterns) runs after each call's analysis completes, after an email
is sent, after a buyer reply is received, once a day while the server runs, and on demand
(`salescoach learn --recompute`, or the button on the page). It is idempotent. A failure is
rolled back, logged and recorded; it never fails the pipeline.

`salescoach learn --show` prints patterns, open proposals and outcome counts.

The new tables carry a `seller_id`, so merging abstracted rows across sellers is possible later.
Nothing does that today.
