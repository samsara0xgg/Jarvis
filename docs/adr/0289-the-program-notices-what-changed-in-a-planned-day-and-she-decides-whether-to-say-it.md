# ADR 0289 — The program notices what changed in a planned day, and she decides whether to say it

**Status:** Accepted
**Date:** 2026-10-16
**Amends:** ADR 0288 (elastic-block reminders, the failure card, the opening's trigger)

## Context

- ADR 0288 keeps one plan per day and drafts it at 04:00. On 2026-10-16 he said the plan itself
  is not what matters: 「重要的是我们把这些工具层做好，这样不管计划是怎么样的，它都可以实现出来」.
  Earlier the same day he called the design too rigid: 「感觉我们现在定的这些都很死…怎么样才可以让它
  在使用的时候处于一个非常灵活的状态」. A 4:00 forecast can differ from the one before he leaves
  (his question, answered with a re-check before each outing).
- What ADR 0288 left as fixed rules: one reminder per movable block at its window start, a
  notice card on a failed draft (a reminder notice pushes at any quiet level, so it would ring at
  night), and nothing that reacts when the day departs from the plan.
- What exists (code on main): `reminders.schedule` and a 15 s ticker that speaks a fixed line
  (ADR 0179); `_say_conversation_line` for a spoken line without a turn; `append_record` writes a
  row the phone draws as hers and the history reads as an assistant message; `Push.send`; quiet
  levels `off < quiet < no-pop < dnd`; `phone.state_observed` (focus, alarm, power) recorded and
  read by nothing; phone stays, moves and sleep folded by `phone_day`; the work state's activities
  (`work_state.revised`); kept places; `plan_leg` (feat/plan-transit) for a leg on a day.
- No proactive source asks a model whether to speak: job mail uses a rule table. A plan cannot be
  judged by rules: whether a late start matters depends on what comes after it.

## Decision

The program only notices; she decides. A watcher compares the day's plan with what the log says
happened, writes each change it notices as `plan.noticed`, and asks a small model, with the plan
and the facts, whether to stay silent, tell him, or change the plan. Three limits stay fixed in
code: quiet levels and sleep, privacy on the lock screen and in pushes, and a cap on interruptions.

- **Notices** (pure, L3 `jarvis/decision/plan_watch.py`, over facts read from the log by L2
  `jarvis/state/plan_facts.py` as of a moment): `woke` (the first sign after 04:00 local: a phone
  signal such as focus off, alarm stopped, charger unplugged or headphones on, the Mac turning
  active, words of his, a ride, or a walk or drive); `not_up` (no such sign 30 min after the first
  item, when it is before noon); `outing_soon` (25 min before a leg's leave time: the weather and
  the leg are fetched again and the notice carries both, old and new); `left_late` (3 min after
  the leave time with no move since, unless he is known to be somewhere else); `item_due` (a
  `movable` item's start or window start, carrying what the work state says he is doing);
  `stayed_longer` (15 min past a placed item's end with no move since, when the next placed item
  is elsewhere, has no leg and starts within 45 min); `left_early` (a drive, a ride or a walk of
  10 min or more began 20 min or more before a placed item's end); `new_commitment` (a
  commitment noted today for today and not in the plan). A move, not a phone visit, says he left:
  visits stopped arriving after 2026-10-10 and location fixes come about every half hour. Each
  notice has a key (kind and item id) and is noticed once per day.
- **Decide** (L3 prompt and parser, skill `day-plan` task `notice`): input is the plan, the notice,
  the facts (where he is, what he is doing, the weather, the legs), the time, and what she already
  told him today. Output `{"act": "silent" | "tell" | "adjust", "say": "...", "changes": [...],
  "why": "..."}`. `adjust` applies the same changes the `plan` tool takes and writes `plan.revised`
  by `her` with `why`; `tell` and `adjust` carry `say`. The answer is logged as `plan.decided` with
  the notice key. Preset `day_plan.decide_preset` (`haiku`).
- **Tell** (L6 `jarvis/runtime/plan_run.py`): her words go into the conversation as her record
  (`plan-<date>-<key>`), are spoken on the Mac when she may speak there, and go to the phone as a
  push when he is away from the Mac. Outing notices (`outing_soon`, `left_late`) obey only speech
  mute, a live conversation and a call, as reminders do. Every other notice obeys the quiet level
  (`quiet` silences the push, `no-pop` and `dnd` keep it in the conversation only) and a cap of 8
  told a day and 2 in any hour; past the cap a `tell` is kept in the conversation unspoken.
  A private item is named by its time only in a push and in a spoken line on speakers.
- **Change by a sentence**: her conversation tool `plan` (`read`, `change`, `settle`) applies
  `add`, `edit` (any field, moving is editing `start`), and `drop` to a day, as one `plan.revised`
  by `him`. How she should plan (his rules) is kept with `keep` like any rule he sets. A one-line
  state block carries the plan's next item and whether it is settled.
- **Opening**: the `woke` notice opens the day as ADR 0288 says, and is not judged. A day whose
  draft failed opens with her saying so; no card is pushed at night.
- **Routes**: the draft and every `outing_soon` compute legs with `plan_leg` in the background.
- **Settle**: the first item's start settles an unsettled plan (`first_item`).
- **Replay**: `scripts/plan_day_replay.py` runs the watcher over a past day of the log with a
  plan from a file, minute by minute, and prints every notice and decision; the decider is a stub
  unless `--live`.
- Off until he says so: `day_plan.watch: false`.

## Alternatives rejected

- **A rule table per notice (as job mail does)** — whether a late start matters depends on what
  follows it; every rule he would want is a special case, which is the rigidity he objected to.
- **Ask the conversation model on every tick** — most minutes nothing changed; a model call per
  minute costs and invites chatter. The program says when something changed; the model decides
  only then.
- **Elastic reminders as fixed lines (ADR 0288)** — a fixed line at a window start says the same
  thing whether he is already on it or deep in something else.

## Consequences

- One small-model call per notice; a day has about 5 to 15.
- `outing_soon` costs one WeatherKit read and one Routes request per placed item.
- Notices and decisions are in the log, so a day can be replayed and her judgement compared with
  what he did.
