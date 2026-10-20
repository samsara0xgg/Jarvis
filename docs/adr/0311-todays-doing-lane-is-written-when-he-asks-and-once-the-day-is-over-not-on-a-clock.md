# ADR 0311 — Today's doing lane is written when he asks and once the day is over, not on a clock

**Status:** Accepted
**Date:** 2026-10-20
**Supersedes:** ADR 0310

## Context

- ADR 0310 writes the doing lane with `claude-opus-5-5` in three kinds of run: a short run
  every two hours while he is awake, a short run when he asks (her `tidy_day`, the
  timeline's `POST /inherent/day/tidy`), and a whole-day run once the day is over, plus
  whole-day rewrites of the last 3 days when late rows change their inputs. Opus was kept
  because Haiku cut 10-19 into 25 spans and Sonnet 5.5 into 8 while dropping an errand;
  he judged Opus's 10 right.
- At $4/$20 per million tokens a whole-day run costs about $0.13–0.15 and a short run about
  $0.05–0.10. The clock's short runs fire whenever the inputs changed, which in a waking day
  is nearly every two hours: about 7 a day, so about $0.8–1.2 a day with the night and the
  late rewrites, $25–35 a month.
- His API credit is $200 a month and was nearly spent this month. Shown these numbers, he
  chose the night's run plus his own asking, with no writing on the clock during the day
  (2026-10-20 18:17Z, decision card), and to turn the clock back on when credit allows.
- He wants what the writer is unsure of asked once, in the evening review, not during the
  day (2026-10-20 18:18Z).

## Decision

Write today's doing lane only when he asks; write on the clock only a day that is over, and
keep the clock's two-hour drafts of today behind a config switch, `doing.clock_drafts`,
shipped off.

- Everything else in ADR 0310 holds: Opus; a run he asks for writes only the time after
  the last written span's start once a draft exists, with the earlier spans as read-only
  context; the whole-day run after the waking day ends; whole-day rewrites of the last 3
  days when late rows change their inputs, at most every two hours; nothing called when
  the inputs did not change; one run at a time; his answers win and attach by time; the
  program's gates send a run back once.
- Until he asks, today is served as the program's segments with their facts and no words,
  as time after the latest run already is.
- With `doing.clock_drafts` true, today is also written at most every two hours, as ADR
  0310 had it.

## Alternatives rejected

- **Keep the two-hour clock (ADR 0310)** — about $0.8–1.2 a day, $25–35 a month, against a
  $200 monthly credit that was nearly spent; he chose against it on these numbers.
- **A four-hour clock** — about $0.5–0.7 a day; still several runs a day he did not ask
  for, and he chose the cheaper option with Opus kept.
- **Haiku or Sonnet for the clock's runs, Opus at night** — on the same 10-19 input Haiku
  wrote 25 spans and Sonnet dropped the phone-plan errand; a daytime draft he cannot trust
  is the timeline he already rejected.

## Consequences

- An unattended day stays wordless on the timeline until he taps 理一下, asks her, or the
  day ends; the night's run is the first words most days get.
- His first ask of the day writes the whole day so far (about $0.14); later asks write only
  the newest part.
- Late-row rewrites of the last 3 days are still on the clock and are the remaining
  unasked cost, up to one whole-day run per day per two hours while rows trickle in.
