# ADR 0173 — One notch card when a day's recorded spend first crosses its limit

**Status:** Superseded-by-0185
**Date:** 2026-10-06
**Supersedes:** none

## Context

- Allen, 2026-10-06, via the coordinator: Jarvis records every model call but
  has no spending limit; he approved adding one and gave no number. The default
  is a guess: recorded spend ran $0.04 to $0.72 a day in early October, about
  $0.24 a day since 9/29, and Allen found a first guess of 3.00 too high, so it
  is 1.00.
- Stopping or switching off a paid model at the limit would silence her mid-day,
  which is a worse failure than the overspend the limit exists to catch.
- `cost.recorded` rows carry `cost_usd` and are written from three places
  (the L3 turn, the cost guard, the worker run), only one of which publishes on
  the committed-event bus, so a bus subscriber would miss most of them. The log
  indexes `(type, ts_epoch_ms)`, and ADR 0164 and commit 20e86317 keep
  whole-log reads off hot paths.
- Text-to-speech is recorded in characters (`tts.usage_observed`), Jev's calls
  go to its own log, and `gpt-live-1` has no price, so none of them have dollars
  in the event log.
- The notch card path is already the job-alert rows of ADR 0155: they honour the
  quiet level (ADR 0153) and the moment's hold (ADR 0161), and the channel-health
  card shows a non-mail alert can use them.

## Decision

Jarvis raises one silent notch card the first time a local day's recorded
`cost.recorded` dollars reach `spend_cap.daily_usd` (default 3.00, overridable in
`settings.yaml`), and does nothing else: no model is stopped, switched or slowed.

Its limits:

- Once a minute, off the loop thread, the sum of today's `cost.recorded`
  `cost_usd` (a null price adds nothing) is read through the `(type, ts)` index;
  the day is the Mac's local day.
- The card is a `job_alert` row keyed `spend-cap-<date>` at level `card`, so one
  exists per day even across a restart, and a new day starts from zero.
- It is served by `JobMail.notices`, so it needs `job_mail.enabled`; a block that
  is absent or not a positive number means no limit.

## Alternatives rejected

- **Stop or downgrade paid models at the limit** — she would go quiet or dumb
  mid-conversation; the one overrun day on record was an overrun of cents.
- **Count in a `CommittedEventBus` subscriber** — two of the three writers never
  publish, so the running total would miss most spend.
- **A second ledger of daily totals** — one index range read of a day's rows
  (hundreds) costs less than keeping a table consistent with an append-only log.
- **A client-made card like ADR 0160's pops** — the client cannot read the event
  log's cost rows; the daemon already owns the alert rows and the quiet level.

## Consequences

- Only priced OpenAI calls are in the total (a row with no price adds nothing).
  MiniMax TTS, Jev on OpenRouter, Tavily, Exa and `gpt-live-1` are not, so the
  real day is higher than the figure on the card.
- A daemon running without `job_mail` raises no card.
- The check is at most a minute late, and a card made at 3.00 is not made again
  if the day later passes 6.00.
