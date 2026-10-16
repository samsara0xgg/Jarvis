# ADR 0288 — Each day has one plan in the event log, drafted before he wakes and opened by her when he does

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** none

## Context

- Planning his day is the first thing he asked of Jarvis (ADR 0275). The design he approved on
  2026-10-15 (doc 「Jarvis 每天的计划：设计」) sets the loop: a draft waits for him in the
  morning, she speaks first, a chat changes it, it settles, the day runs on it. On 2026-10-16 he
  put the morning plan ahead of chasing hard items ("计划更重要") and said 「可以」 to: draft at
  04:00 while there is no evening review, goals seeded from what he said and kept in her memory,
  Sonnet for the draft, a dry run on five past days before it opens, two phone changes, and three
  Shortcuts automations he sets up.
- What exists: commitments from mail and his words (ADR 0274); routines from what he did
  (ADR 0279, empty until visits return); the day's items folded from the log (ADR 0199);
  Microsoft To Do and calendar behind `my_schedule`; WeatherKit for any place and day
  (ADR 0285); reminders that fire and catch up after a restart (ADR 0179); APNs pushes; a phone
  timeline that draws kind `event` from every build. Goals live in core memory section
  「生活里的几件事」 (ADR 0146); hard deadlines in 「证件和硬期限」.
- What is missing, from the code on main: no planner state or job; the bus planner takes only
  today and a departure time (no date, no arrive-by) and shows a chat card as a side effect;
  nothing writes a message of hers into the conversation without a turn of his; a push opens
  the conversation nowhere; nothing reacts to `phone.state_observed`. The phone already sends
  focus off, alarm stopped and charger unplugged through `TellJarvisIntent` once he adds the
  Shortcuts automations; the brain records them and does nothing.
- A plan the program cannot read back is only text: times must not collide, legs must be
  computed, and his changes must be kept so planned and lived days can be compared later.

## Decision

Keep one plan per local day as event-log state, written by three hands: a 04:00 drafting job
(Sonnet, through a planning skill written in her voice), her conversation tool `plan` when he
asks for a change, and the program (routes, settling, catch-up); the first wake signal of the
day refreshes the draft and puts her opening into the conversation as her message.

- **State (L2, `jarvis/state/plan.py`)**: `plan.drafted` (date, items, the inputs' digest,
  model), `plan.revised` (date, full items, `by` him / her / program, why), `plan.settled`
  (date, `by` him or `first_item`), `plan.opened` (date, record id, the signal and its time),
  `plan.draft_failed` (date, error class). The last drafted or revised items of a date win.
  An item: id, title, start `HH:MM` or none, minutes, an optional window (from, to), place,
  `firmness` (`fixed`, `movable`, `droppable`), why, goal, prepare (list), source
  (`commitment:<id>`, `todo`, `goal`, `routine:<id>`, `his`), private, and the computed route
  (leave at, line, stops, arrive at, a backup) when it has a place.
- **Draft (L3 prompt and parser, L6-wired job)**: once a day at `day_plan.at` (04:00 local),
  and on start when today has no draft yet. Inputs: commitments of the next 3 days (confirmed or
  not), calendar and To Do due items, the two memory sections, routines of the day, the
  weather, and what the last 3 days held (ADR 0199). The answer is untrusted JSON: an item that
  does not parse, collides with a `fixed` one or names an unknown source is dropped and noted;
  commitments the model left out are added back as `fixed`. The program then computes the
  legs between consecutive placed items, backwards from `fixed` starts. A failed draft writes
  `plan.draft_failed` and a notice card 「今天的草稿没排出来」, never an empty day.
- **Routes**: the bus planner gains a day and an arrive-by time, and a background call that
  shows no card and keeps no offer.
- **Opening**: the first of these after 04:00 on a day with a draft and no opening opens it:
  focus off, alarm stopped or snoozed, charger unplugged, the phone link connecting, or a turn
  of his. She refreshes weather and legs, writes the opening with the same skill, and the
  program appends it to the conversation as her record (`plan-open-<date>`, idempotent) and
  writes `plan.opened`. A push whose tap opens the conversation goes with it only while
  `day_plan.morning_push` is on; it stays off until his signals have arrived on time for
  several mornings. A morning with no signal gets a silent push before the first outing, under
  the same switch.
- **Change and settle**: her tool `plan` reads a day and applies his changes (add, move, drop,
  edit, settle) as one `plan.revised` or `plan.settled`; the state block carries one line with
  the plan's next item and whether it is settled. With no answer from him, the program settles
  it when the first item starts.
- **Elastic blocks**: a `movable` item with a window gets one reminder (ADR 0179) at the
  window's start, at most 8 plan reminders a day and 2 in any hour; the reminder says what and
  until when, and a private item says only the time.
- **Phone**: day items of source `plan` are kind `event` with `source: "plan"`; an item that
  stands for a commitment is drawn once, as the commitment. The phone labels them 「计划」.

## Alternatives rejected

- **Plan as a skill the conversation model runs when he asks** — nothing is ready when he
  wakes, which is the first thing he asked for; and a plan that exists only in a reply cannot
  be compared with the day he lived.
- **One model call that also computes the buses** — the model has no timetable: every leave
  time it wrote would be a guess, while the Routes API answers with real departures for a given
  day; and legs change with a delay or a moved item without a new draft.
- **Write the plan into his Outlook calendar** — the approved design keeps soft items out of
  his calendar (doc, 「这次不做」): a movable block in Outlook reads as a fixed appointment on
  every device he owns.
- **Open with a push alone, not a message in the conversation** — a push is gone when tapped;
  his answer has to land in a conversation that already holds her opening, or the next turn
  answers a message the model never saw.

## Consequences

- A draft costs one Sonnet call a day (estimated 0.1 to 0.3 USD); the opening one more call
  of the conversation preset. Both are recorded as `cost.recorded` like every model call.
- Legs cost Google Routes requests under the maps monthly cap (ADR 0283): a day of five placed
  items is about five calls at draft and five at opening.
- Her messages now exist without a turn of his; history building already accepts an assistant
  row first (llm_anthropic), and the phone shows any non-his record as hers.
- Until routines return and the evening review exists (step 4), the draft leans on
  commitments, To Do and the memory sections; settled-versus-lived comparison waits for step 4.
