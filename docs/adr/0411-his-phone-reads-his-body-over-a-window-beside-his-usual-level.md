# ADR 0411 — His phone reads his body over a window beside his usual level

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- The new phone and iPad day page (thread 手机端 UI 落地) has a 「跟平时比」 on a stretch of his
  day: how his steps, heart, breathing and sleep went then, against what is usual for him.
- `GET /inherent/day`'s `summary` (ADR 0293) has three numbers for a whole day and nothing to
  compare them with. The `body` tool (ADR 0388) has the comparison, for her: every Health measure
  over a day, days or hours of one day, beside his usual level for the same hours of the 14 days
  before, as JSON and words she reads aloud.
- A stretch of the page can cross midnight (a late evening, a night), which the tool's
  arguments cannot name: its hours belong to one day.
- The phone draws numbers, not sentences, and must not ask her (a model turn) to draw a card.

## Decision

`GET /inherent/body?start_ms=&end_ms=` is a read-only route, guarded as `/inherent/day` is,
serving `steps`, `heart_rate`, `resting_heart_rate`, `hrv`, `breathing_rate`, `blood_oxygen` and
`sleep` over a window of at most 25 hours, each with its value, its usual level and where it
stands against it, all as numbers.

- One rule for every measure's usual level, said in the answer's `baseline`: the window's clock
  hours moved back one to 14 days in the owner's zone, their median and middle half when at
  least 3 of those days hold any. Within one day these are the tool's hours, so the phone and
  she give the same numbers; the rule (`body.Usual`, its 14 days and 3 days) moves to L2, where
  the tool and the route both read it.
- A reading is the median of the readings in the window; steps are Health's hourly totals of the
  hours it touches, and to now the days before count the current hour as far as it has passed,
  as the tool does; sleep is the minutes asleep of the sleeps he woke from in it.
- The route judges nothing: below, within or above the middle half is the whole verdict.

## Alternatives rejected

- **Serve the tool's answer.** Its usual level is words ("71 to 77 bpm", "7h 30m") made to be
  read aloud, and its windows cannot cross midnight.
- **Every measure the tool knows.** The page asked for these; more come when a card needs one,
  and a client keeps what it does not know.
- **His usual level over whole days for a stretch of hours.** An afternoon's heart rate against
  whole days mixes in his nights; the tool already compares the same hours.

## Consequences

- One request reads three kinds of rows over 15 days of the log, as one tool call does.
- For a window across midnight, the tool and the route can differ: the tool cannot be asked for
  it, and the route sets it beside the same hours of the nights before.
- Sleep here is the sum of his sleeps in the window, naps included; the tool's one-day sleep is
  his longest night. The phone names it as the total.
