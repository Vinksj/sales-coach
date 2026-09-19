# Sales methodologies

The deal strategist scores every deal against a list of **elements**. Which list is a choice you
make once, in Setup step 2. A methodology is data, not code: nothing in the Python names an
element. The code asks `salescoach.intel.methodology.active()`.

- `config/methodologies.yaml` is the built-in library (tracked): `meddpicc`, `meddic`, `bant`,
  `spiced`, `spin`, `challenger`, `sandler`, `command_of_the_message`. Its `default:` is `meddpicc`.
- `data/settings/methodology.yaml` is yours: `{active: <key>, custom: {<key>: <definition>}}`.
- MEDDPICC is also defined in code as a fallback. A missing or broken YAML file, or an `active`
  key that no longer exists, falls back to it instead of stopping the pipeline. An invalid
  definition is skipped and logged; the rest still load.

## What a methodology changes

| Where | Effect |
|---|---|
| Deal strategist | The prompt lists the active elements, what "known" and "partial" mean for each, two typical questions, the health rubric and the usual bottleneck. The output schema is built from the element keys, so the model must answer for exactly those elements |
| Evidence | An element is recorded as **known** only with a verified buyer quote at medium confidence or better, and **partial** only with at least one verified quote. Otherwise it is downgraded, with a note. This holds for every framework |
| Deal health | The model's score is lowered to the smallest applicable cap, and each applied cap is shown with its reason (see below) |
| Risks | An element with `risk_when_unknown` raises that risk type in code while the element is unknown |
| Deal page, prep brief | Show the active elements; gaps are listed in `gap_order` |
| Call analyst | Receives `coaching.analyst`, and may tag a gap with this framework's `lens` or one of `secondary_lenses` |
| Live coach | Receives `coaching.live`; `coaching.live_weights` replace trigger weights from `config/live_coach.yaml`. Your own `live_coach.yaml` still wins over both |

### Conversation frameworks

`kind: conversation` (SPIN, Challenger, ...) describes how a selling conversation should go, but
every element is still a fact about the **buyer**. The strategist is told to judge an element only
by what the buyer said. "The seller asked an implication question" can never be an element; "the
buyer stated a consequence" can.

### Health caps

Deal health is capped by facts, in code (`intel/strategist.py: health_caps`):

1. **One buyer-side voice**: at most one buyer-side person has been on any call
   (`single_threaded`, from `config/intel.yaml`).
2. **Each critical element that is not known**: `cap_when_not_known`, under the name `cap_rule`.
3. **No dated buyer commitment**: no open buyer-owned loop with a due date
   (`no_dated_buyer_commitment`, from `config/intel.yaml`).
4. **Too few elements known**: `health.min_known` (`count`, `cap`, `rule`).

Caps 1 and 3 do not depend on the methodology. For 2 and 4 the methodology supplies the number,
and `intel.yaml health.caps[<rule name>]` overrides it when present (the install's own tuning).

## The format

```yaml
methodologies:
  <key>:                  # [a-z0-9_]+, at most 40 characters, no colon
    name:                 # required; what people call it (max 60)
    lens:                 # short tag the analyst puts on a gap (max 24; no / [ ] | or line break). Default: the name
    kind:                 # qualification (default) | conversation
    description:          # one paragraph for the setup page (max 1500)
    elements:             # 2 to 12, in display order
      - key:              # [a-z0-9_]+; not "health"; must not start with "risk"; unique
        label:            # required; what the seller reads (max 40)
        known_when:       # required; the fact the buyer must have confirmed on a call (max 600)
        partial_when:     # what counts as half way (max 600)
        questions: []     # how to ask for it; at most 6, each max 240. The first two reach the strategist
        critical: false   # true: not knowing it caps deal health
        cap_when_not_known:   # 0 to 100; required when critical, not allowed otherwise
        cap_rule:         # the cap's name in the health record. Default <key>_not_known. Unique; not a reserved name
        cap_why:          # the reason shown to the seller (max 200). Default "<label> is not confirmed by the buyer"
        risk_when_unknown:    # one of the risk types below
    gap_order: []         # element keys, most important gap first. Default: critical elements, then element order
    health:
      min_known: {count: , cap: , rule: }   # fewer than `count` known caps health at `cap`. rule default <key>_known_below_<count>
      rubric: |           # how the strategist scores 0 to 100 (max 2000). A default is generated from the critical elements
      bottleneck_hint:    # where deals under this framework usually stall (max 300). A default is generated
    coaching:
      analyst:            # added to the call analyst's prompt (max 2000)
      secondary_lenses: []    # other lens tags the analyst may use; at most 6
      live:               # added to the live coach's prompt (max 1200)
      live_weights: {}    # nudge trigger -> weight from 0 to 1
```

Unknown fields are errors. No text field may contain `{{` or `}}` (those are prompt variables).
Reserved cap rule names: `single_threaded`, `no_dated_buyer_commitment`.

Risk types: `single_threading`, `weak_champion`, `no_economic_buyer_access`,
`undefined_decision_process`, `weak_urgency`, `status_quo`, `competition`, `procurement`,
`technical`, `political`.

Live nudge triggers: `dig_deeper`, `quantify_impact`, `root_cause`, `status_quo`,
`buying_process`, `stakeholder_gap`, `weak_commitment`, `buying_signal`, `objection`.

### Shared keys

Use the **same key** wherever two frameworks mean the same thing: `champion`, `economic_buyer`,
`decision_process`, `identify_pain`, `metrics`, `budget`, `situation`. Rows are stored per deal
and element key, so what is known about a deal carries over when you change methodology. A row
whose key is not in the active methodology is simply not read: switching hides it, switching back
shows it again, with your own edits still protected.

## A full example: BANT, as shipped

```yaml
bant:
  name: BANT
  lens: BANT
  kind: qualification
  description: >-
    Budget, Authority, Need, Timeline: four questions that tell you quickly whether an opportunity is
    worth pursuing now. It fits high-volume and inbound selling, SDR qualification and shorter cycles
    with one or two decision makers. It is thin for enterprise deals, where budget is often created
    for a strong case rather than found, and where many people share the authority.
  elements:
    - key: budget
      label: Budget
      known_when: >-
        The buyer said money is allocated or can be found for this, and gave a figure, a range or the
        budget it would come from.
      partial_when: >-
        The buyer said budget should not be a problem, or that it depends on the business case, with
        no figure and no source.
      questions:
        - "Is there a budget set aside for this, or would it need to be found?"
        - "What range did you have in mind when you started looking?"
    - key: authority
      label: Authority
      known_when: >-
        The buyer named who makes the final decision and who else must agree, and the decision maker
        has engaged directly or sent a clear position.
      partial_when: >-
        A decision maker was named but has not engaged, or the contact says the decision is theirs and
        nothing confirms it.
      questions:
        - "Besides you, who is involved in deciding this?"
        - "Who has the final say when it is time to sign?"
      critical: true
      cap_when_not_known: 60
      cap_rule: authority_not_confirmed
      cap_why: the person with authority to buy is not confirmed and engaged
      risk_when_unknown: no_economic_buyer_access
    - key: need
      label: Need
      known_when: >-
        The buyer described a specific problem or goal the offering addresses and said it is a
        priority for them now.
      partial_when: There is general interest, but no specific problem, or no sign that it is a priority.
      questions:
        - "What made you look at this now?"
        - "Where does this sit among the things you have to get done this quarter?"
      critical: true
      cap_when_not_known: 45
      cap_rule: need_not_confirmed
      cap_why: the buyer has not confirmed a specific need that is a priority
      risk_when_unknown: status_quo
    - key: timeline
      label: Timeline
      known_when: The buyer gave a date by which they need this in place, and the reason behind that date.
      partial_when: A rough timeframe was mentioned with no reason behind it, or it keeps moving.
      questions:
        - "When do you need this working by?"
        - "What happens if it slips past that date?"
      risk_when_unknown: weak_urgency
  health:
    min_known: {count: 2, cap: 50, rule: bant_known_below_2}
    rubric: |-
      - 0 to 24: none of the four is confirmed; a conversation, not an opportunity.
      - 25 to 49: a need is stated, but there is no access to the person with authority and no timeline.
      - 50 to 69: need and authority confirmed, a next step with an owner and a date.
      - 70 to 84: all four confirmed by the buyer, and the timeline has a reason behind it.
      - 85 and above: verbal commitment or paper in motion.
    bottleneck_hint: usually no access to the person with authority, a need that is not a priority, or a timeline with nothing behind it
  coaching:
    analyst: >-
      This team qualifies with BANT. When you look for gaps, check the four first: is there money
      (Budget), who decides and have they engaged (Authority), is there a specific problem that is a
      priority now (Need), and is there a date with a reason behind it (Timeline). "Budget won't be a
      problem" and "sometime next quarter" are not answers; say so when the seller accepts them.
    secondary_lenses: [SPIN, Challenger, Gap]
    live: >-
      This team qualifies with BANT. Prefer nudges that get one of the four from the buyer's mouth: a
      figure or a budget source, the name of the person who signs, why this is a priority now, the
      date and the reason for it. A vague answer on any of them is worth one follow-up.
    live_weights: {buying_process: 0.9, stakeholder_gap: 0.85, weak_commitment: 0.95}
```

Read it as: with BANT active, a deal where the buyer has not confirmed a priority need cannot
score above 45, one without confirmed authority cannot score above 60, and one with fewer than two
known elements cannot score above 50, whatever the model thinks.

## Writing your own

**In the UI.** Setup > How you sell > build your own. The form covers name, key, kind,
description, and per element: label, key (derived from the label when left empty), known when,
partial when, questions (one per line), critical, and the cap. Errors are shown beside the field
that caused them. Saving does not activate it unless you choose to.

**By hand.** The form does not show `lens`, `gap_order`, `health`, `coaching`, `cap_rule`,
`cap_why` or `risk_when_unknown`, and preserves them when you edit through the form later. Add
them in `data/settings/methodology.yaml`:

```yaml
active: land_and_expand
custom:
  land_and_expand:
    name: Land and expand
    kind: qualification
    elements:
      - key: identify_pain            # shared key: what MEDDPICC already learned carries over
        label: Pain
        known_when: The buyer described a specific problem, who feels it and what it costs.
      - key: champion
        label: Champion
        known_when: A person with influence has acted for the deal when the seller was not in the room.
        critical: true
        cap_when_not_known: 55
      - key: expansion_path
        label: Expansion path
        known_when: The buyer named the next team or site that would adopt this if the first one works.
        questions: ["If this works here, where would it go next?"]
```

A custom key cannot be the key of a built-in methodology. Guidance for good definitions:

- Write `known_when` as something the **buyer** says or does, observable in a transcript. The
  evidence check only accepts buyer quotes.
- Make an element critical only if a deal genuinely cannot close without it; each critical
  element is a hard ceiling on health.
- Keep `coaching.analyst` and `coaching.live` short and concrete. They are appended to prompts
  that already describe the job.

## Switching

`methodology.switch` (what the Setup page calls) sets the active key and queues a strategist run
for every active or paused deal that has an analysed call. The strategy cache is keyed on the
rendered prompt, which names the elements, so no run is served from a strategy made under another
methodology. One accepted gap: the deal health history gets no marker at a switch, so a health
line can step because the caps changed, not because the deal did.

A historical name remains: the table that stores elements is called `meddpicc` whatever the
framework.
