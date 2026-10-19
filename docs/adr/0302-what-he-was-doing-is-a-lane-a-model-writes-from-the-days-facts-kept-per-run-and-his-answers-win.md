# ADR 0302 — What he was doing is a lane a model writes from the day's facts, kept per run, and his answers win

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** 0199

## Context

- ADR 0199 serves each day as structured items, folded from state on each read, with no
  stored timeline and no guesses: "a gap in the signals is a gap in the line". The line
  now has sixteen kinds of facts. None of them says what he was doing. On 10-17 it
  shows a stay at 1150 McKenzie from 13:37 to 14:14. The ICBC booking mail and the
  picture he sent that night say the stay was his knowledge test, and that he passed.
- On 2026-10-18 and 10-19 he asked for each waking minute to be filled automatically as far
  as the data allows. He asks only about the rest, in one nightly review he agreed to
  answer (10-19 18:21Z). He approved a timeline that draws one story line of what he
  was doing, grouped by place, with Mac use, calls and the body shown as marks on it
  (10-19 06:52Z).
- Judgement belongs to the model; code computes facts (10-11). Saying what a stretch
  of time was needs a model: that "two hours at UVic" and his line "下午打羽毛球" from
  the day before are the same event. Its boundaries, coverage and clues are facts.
- A model's account is not evidence (ADR 0142), and his own words and marks win (ADR
  0235). The fact lanes have readers that count them as facts: the places tally (ADR
  0234), routines (ADR 0279) and the ledger (ADR 0201).
- The day page and the Mac panel read a day many times an hour. A Haiku call
  (`claude-haiku-5-5`, $0.10 per million input tokens) takes seconds. A whole day's
  facts and clues fit in one call of about 30k tokens.
- Late phone batches change a past day hours later (ADR 0301). The screens need span
  ids that stay the same across refreshes, and an answer he gives must survive the
  next rewrite.

## Decision

Serve each day as fact lanes folded on read, as ADR 0199 did. Add one `doing` lane, which
a model writes from those facts and is kept per run. His answers are kept apart from the
runs and win over them.

- **Fact lanes stay facts.** The items of ADR 0199 keep its rules:
  - they are structured and owner-local;
  - each source has one fold, and a source that cannot be read is named in `missing`;
  - nothing is guessed, and a gap stays a gap.

  The same facts are also served as spans in three lanes: `place`, `use` and `body`.
  Their basis is `observed`.
- **Code cuts, the model words.**
  - The program cuts the waking day into segments at the boundaries of the fact lanes.
    It folds a segment shorter than 10 minutes into its neighbour. Every waking minute
    belongs to exactly one segment, and a minute that no fact covers is in a segment too.
  - The program gathers each segment's clues: his words and hers, pictures, mail
    commitments, calendar, plan, payments, Claude chats and sessions, screen summaries,
    and his past answers at the same place.
  - One Haiku call for the day answers, for each segment:
    - what he was doing;
    - a category from a closed list;
    - its basis: `observed`, `stated` or `inferred`;
    - its confidence;
    - the clues it used;
    - optionally, a question to ask him.

    The call may split a segment at the time of a clue.
  - A segment that nothing speaks to is `unknown`. It is never left out.
- **Runs are kept.**
  - Each run of a day is appended as a whole. The latest run of a day is the one served.
  - A run is drafted at most hourly while the day is under way, and only when its inputs
    changed. A final run is written after the day ends.
  - A past day is written again when late rows change its inputs.
  - A run that fails or is gated out stores nothing, and the previous run stays.
  - Time after the latest run is served as program-cut segments with their facts and
    no words.
  - After now, the day's plan is served on the same lane with basis `planned`.
- **His answers win.**
  - An answer, whether from the timeline or from the nightly review, is stored apart
    from runs.
  - It attaches by time, not by span id. Every later run overlapping that time shows his
    words, with basis `stated` and `confirmed` true.
  - It is a clue to the next run at the same place.
- **Questions.**
  - The model picks which uncertain segments to ask about. A day holds at most five
    questions.
  - They wait for the nightly review, whose delivery belongs to his ledger line. Nothing
    asks during the day.

## Alternatives rejected

- **Fill the gaps of the fact items with guessed items** — the places tally, routines and
  the ledger read those items as facts. A guessed stay would count as a visit and a
  guessed work block as Mac time.
- **Write on each read** — the page is opened many times an hour. A Haiku call on the
  request path adds seconds to each open and a charge each time. ADR 0244 rejected an
  on-demand summary for the same reason.
- **One call per segment** — about 40 segments a day, each repeating the day's context.
  One call per day also sees the neighbours: a picture at 21:00 says what the afternoon
  was.
- **Let the model cut the day** — boundaries are computable from stays, moves and work
  blocks. A model's cut cannot be checked minute by minute for coverage, and it moves
  between runs, which breaks stable ids.
- **Key his answers by span id** — late fixes re-cut a segment's edges, so an id-keyed
  answer is orphaned by the next run. Overlap in time survives the re-cut.
- **Keep ADR 0199 and add the lane in a separate ADR** — 0199 says the brain stores no
  timeline and never guesses. This lane does both, so 0199 can no longer stand as the
  rule of the day.

## Consequences

- A day has stored, model-written rows that a newer run can contradict. Older runs stay
  only as history.
- Writing costs Haiku calls: at most one per waking hour when inputs changed, plus one
  each night, on the order of cents a day.
- `GET /inherent/day` grows `spans` and `coverage`, and a `POST /inherent/day/answer`
  route is added. The items stay unchanged for readers that use them.
- `my_day`, the day prose, the plan's recent days and recall still read the fact items
  until they are moved to the doing lane, so for a while they say less than the
  timeline does.
- A day the Mac's terminal and the phone did not reach is mostly `unknown` until late
  rows arrive.
