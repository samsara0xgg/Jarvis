# ADR 0383 — Unprompted plan lines are four kinds gated in code, and never follow up

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** 0291
**Amends:** ADR 0288 (the opening, settling)

## Context

- On 2026-10-21 he asked for plan tracking that knows roughly how far along he is, gives a morning
  plan and an evening summary, answers his hard questions, and is not annoying. He called the
  ADR 0291 watcher 「非常 annoying」. Unprompted lines have been off on the Pi since 2026-10-19
  (`day_plan.watch: false`). On 2026-10-23 he picked 「按建议，动手」 on the design card.
- The event log of its first full day (`plan.decided`) holds 10 unprompted lines: the opening, 6
  follow-ups chasing a "settled" answer (the 10/15/20/30 min ladder), 2 progress tells ("N minutes
  left, it does not fit") and 1 useful line, a new fixed-time appointment. The next two days had 8
  and 4. No plan was ever settled and he never edited one. The progress tells came from a draft
  whose minutes never moved, because the done-check rarely saw progress. His complaint: she asks
  what she could check herself.
- Research digests (not papers opened): batching into at most 3 windows a day (Fitz 2019) and
  deferring to breakpoints (Iqbal & Bailey 2008); repeated prompts lose response (Ancker 2017);
  numbers shown back help (Harkin 2016), feedback aimed at the self backfires (Kluger & DeNisi
  1996). Sunsama, ChatGPT Pulse and Google CC touch the user once or twice a day at a set time; the
  rest is pull.
- Every kind of line can be named in advance; whether a line is due is a fact about the log (he
  is up, a thing was added, he is heading home), not a judgement. What the line says is.
- Signals the brain has (Pi backup, 2026-10-23): phone power, headphones and call; TimeSink's Mac
  states (active, idle, lock, sleep and their resumes); phone stays, moves and bus rides. The
  phone sends no unlock. A thing's writer (`by`) says whether it came from his own words. On
  10-17..10-22 his first sign of the day came between 05:41 and 09:24; his Mac's last lock came
  between 23:29 and 01:36, and evening locks and idles of 10 to 25 minutes (19:01 to 21:14)
  were breaks, not the end of the day.

## Decision

The program says something unprompted about his day only as one of four kinds of line, each
decided due by code from the log, at most four a day in all, and never chases an answer; the
model only writes the words of the morning and the evening line.

- **Morning**: once, at the first sign he is up (the wake signal ADR 0291 defined: a phone
  signal, the Mac in use again, his words, a ride or a walk or drive), before 17:00. The model
  gets the day's plan with its fixed-time things and leave times, and writes a short message:
  the fixed-time things and when to leave, at most three steps in order, and one if-then cue.
  It asks nothing.
- **New hard thing**: a thing with an hour today or tomorrow that mail or the calendar mirror
  added or moved (`by` `mail` or `mirror`) after the morning line, said by a fixed zh/en line. A
  thing from his own words (`her`, `pass`, `him`) is never told back to him.
- **Leave-by**: a ring on the thing, at the leave time of its computed leg, for a go-thing with an
  hour and a place; it replaces only a ring the ledger gave by default, never one he asked for.
  It fires as rings do (ADR 0179), with sound.
- **Evening**: once, at wind-down: home for 10 minutes after being out, from 20:00; or the Mac
  locked or asleep for 20 minutes, from 21:00; failing both, an hour before `day_plan.day_ends`. Code computes planned against done;
  the model writes the message (done, one fact, tomorrow's first step, at most one question) and
  gives each task not done a destination, `tomorrow`, `later` or `drop`, logged as
  `plan.closed`. Tomorrow's draft gets yesterday's `tomorrow` tasks.
- **Limits**: morning and evening once a day each; new-hard-thing and leave-by once per thing,
  at most two of them a day together; his own reminders do not count. No follow-up, no re-ask:
  a line nobody answers is dropped and the plan stands as drafted. There is no settling; she
  never asks where he is, whether he boarded or paid. When he says a kind is not wanted, her
  `plan` tool mutes it (`plan.muted`), and the day reply shows what is muted.
- **Delivery**: every line goes into the conversation as her record. Morning, evening and new
  hard thing are a silent push when he is away from the Mac and are not spoken; `no-pop` and
  `dnd` keep them in the conversation only, and a call holds them to the next minute. The day
  reply carries her latest line of today beside the day prose's `line`.
- The watcher's other notices, the done-check, the progress tells, `plan.decided`,
  `day_plan.decide_preset`, `told_per_day`, `told_per_hour` and `morning_push` go.
  `day_plan.watch` stays the switch for unprompted lines, off until he turns it on.

## Alternatives rejected

- **Keep ADR 0291 with tighter caps** — on 2026-10-17 the cap of 8 was not reached and the day
  still had 10 lines: 6 of them were follow-ups the caps did not count, and the 2 progress tells
  rested on a done-check that did not move. The noise came from the kinds, not the count.
- **Let a model decide whether each moment deserves a line** — that was 0291's design; its
  first day kept 1 useful line out of 10, and a model asked "should I speak" on every notice
  speaks.
- **Morning and evening only when he starts them** (the card's 再少一点) — he named the morning
  plan and the evening summary as the two he wants most, and a pull-only morning is not there
  when he wakes; it stays one tap away as the mute.
- **Morning at a clock time** — his first sign of the day ranged from 05:41 to 09:24 on
  10-17..10-22; a fixed time lands before he is up or after he started.
- **Evening at the first leaving move or Mac lock after 17:00** — on 10-17 a walk at 17:15 led
  to five more hours out, and on 10-21 a lock at 20:53 was followed by two more hours of work.

## Consequences

- Two model calls a day, the morning and the evening, on `day_plan.line_preset` (Haiku), beside
  the nightly Sonnet draft: about 0.1 USD a day in all.
- Without a measure on each plan item (a later step), "done" in the evening is only what he or
  she marked and the ledger closed; the evening says less than it will.
- A leave-by ring needs the ledger to tell a default ring from an asked one; things written
  before that field are treated as asked, so they keep their rings.
- Wind-down is a guess from signals: on most of his days so far it is the fallback hour.
