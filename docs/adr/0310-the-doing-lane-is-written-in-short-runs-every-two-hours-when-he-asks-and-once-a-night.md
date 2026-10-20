# ADR 0310 — The doing lane is written in short runs every two hours, when he asks, and once a night

**Status:** Accepted
**Date:** 2026-10-20
**Supersedes:** the clauses of ADR 0302 that draft a day under way at most hourly, rewriting the whole day each time, with Haiku. Everything else in ADR 0302 stands.

## Context

- ADR 0302 drafts the doing lane at most hourly while a day is under way, rewriting the
  whole day each time, with `claude-haiku-5-5`, and writes a final run after the day ends.
- Hourly whole-day rewrites move under him. On 2026-10-19 the 22:19 draft cut
  19:00–22:10 into 2 spans and the 23:20 draft cut the same time into 7, with the same
  code and nearly the same inputs.
- The grain is the model's judgement more than the prompt's: the prompt has one loose
  rule on it ("most last from half an hour to a few hours"). On one fixed input (10-19 as
  of 10-20 00:30, 16 segments, 73 clues), Haiku wrote 25 spans, Sonnet 5.5 wrote 8 and
  lost a 20-minute errand, and Opus 5.5 wrote 10. He judged Opus's day right (10-20
  07:37Z). Opus wrote 3,263 output tokens against Haiku's 14,299; at $4/$20 per million
  tokens a whole-day Opus run costs about $0.11.
- His phone draws the day bar at about one point per minute. A span shorter than about
  half an hour shows no icon on it, so a fine cut reads as slivers (10-19 23:41 screenshot).
- He wants to ask Jarvis to 「理一下」 by voice or by a tap on the timeline and have it
  done on the spot (10-20 07:51Z), and the day still tidied on a timer without his asking
  (10-20 08:04Z), and said to build it (10-20 08:08Z). His API credit is $200 a month and
  was nearly spent this month.
- ADR 0302 rejected writing on each read: an automatic call on every open adds seconds and
  a charge to each of many opens a day. An explicit request happens far less often, and he
  expects the wait.
- The program's segments are computed from facts and do not move between runs; the
  model's words and splits are what move.

## Decision

Write the doing lane's words with `claude-opus-5-5` in three kinds of run: a short run
every two hours, a short run when he asks, and a nightly whole-day run; drop the hourly
whole-day drafts.

- **Nightly run.** After the waking day ends, one run rewrites the whole day, as ADR 0302's
  final run does, and may re-cut, merge and rename anything except what he answered.
- **Short runs.** A short run writes only the time after the last written span's start.
  Earlier spans are passed in read-only as context, so names stay the same; the last
  written span may grow or be renamed. If nothing changed since the last run, nothing is
  called.
- **When short runs happen.** Every two hours while he is awake, and at once when he asks
  (a tap on the timeline, or asking her).
- **Between runs** the newest time is served as the program's segments with their facts
  and no words, as ADR 0302 already serves time after the latest run.
- Triggering on events (leaving the Mac, arriving somewhere, a call ending) instead of a
  timer is a later decision, not this one.
- His answers still win and attach by time (ADR 0302). Hard rules (cover the window, no
  overlap, cut only at given times, Mac and non-Mac time never joined) stay program gates
  that send a run back once.

## Alternatives rejected

- **Keep hourly whole-day drafts** — the same evening came out as 2 spans and then 7 an
  hour later (10-19 22:19 vs 23:20); a rewrite he did not ask for moves what he already read.
- **Short runs every half hour** — about 32 a 16-hour day; even at about $0.05 a run that
  is about $1.60 a day, against about $0.40 for every two hours, while the bar already
  cannot show pieces under half an hour.
- **Only on his request and at night** — he asked for the day to be tidied without his
  asking too (10-20 08:04Z); an unattended day would stay wordless until night.
- **Refine automatically on every open** — the request path waits for a model that writes
  for seconds to tens of seconds, and the bar changes while he reads it; ADR 0302's reason
  against write-on-read still holds for automatic calls.
- **Sonnet 5.5 for the runs** — on the same input it merged 09:15–12:44 into one span and
  dropped the phone-plan errand that Haiku and Opus both kept.
- **Haiku with a tighter prompt** — 25 spans on 10-19 against a prompt that already says a
  switch of app is not a span; the grain is the model's judgement, not missing words.

## Consequences

- Up to two hours of the newest time shows wordless program segments until the next
  run or his request.
- Each run costs Opus prices: about $0.05 a short run and $0.11 a whole day, about
  $0.50 a day plus what he asks for.
- His asking waits for the model, up to about a minute, in her turn (`tidy_day`) and on
  the timeline's route (`POST /inherent/day/tidy`); one run is written at a time, so a
  request during the clock's run waits for it. The phone's button is the timeline's to draw.
- The grain rules move into a written rule set that both runs share; until it exists the
  grain rests on Opus's own judgement.
- Zooming into a stretch (projects inside it, possibly overlapping) is not decided here.
