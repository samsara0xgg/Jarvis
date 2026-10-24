# ADR 0382 — A data source that stops arriving while it should produce is found by the nightly check and carded

**Status:** Accepted
**Date:** 2026-10-22
**Supersedes:** none

## Context

Extends ADR 0324 and amends ADR 0185's serving condition (see Decision). On 2026-10-17 two of the Mac's sources broke without a sign: the microphone
(after a sleep the terminal logged `output_unavailable`, kept no recording piece, and none came
for 36 hours) and TimeSink's screen text (every OCR run failed from about 16:00Z until 10-19
04:53Z, 3,811 failed runs). A manual audit found both on 10-18. The log holds no event for either:
nothing about the microphone's state reaches the brain except the pieces themselves, and
TimeSink's capture health is only a table in the Mac copy (`imports/shipped.db`, ADR 0294).

- A silence is only meaningful against a precondition. The Mac sleeps, locks and idles for
  most of a day: of the 390 hours from 10-07 to 10-23, TimeSink was in a state to capture for 123
  and the Mac was awake for 329.
- `mac.sleeping` / `mac.awake` stopped on 10-10 (the last `mac.awake` is 10-10 03:28Z), so they
  cannot gate anything. The Mac copy has every TimeSink state event (`sleep`, `wake`, `lock`,
  `unlock`, `idle`, `active`, `stop`, `start`, `tracking_pause`, `tracking_resume`).
- Normal arrival gaps, counted in the time the source should be producing, 10-07 to 10-22:
  spans at most 29 min (p99 4 min), OCR rows at most 20 min, recording pieces at most 41 min,
  apart from the breaks below. The longest silences: spans and OCR 517 min on 10-09, OCR 739 min
  on 10-17/18, the microphone 1,381 min on 10-17/18.
- The brain hears a sleeping Mac late. The Mac ships every 5 minutes, but TimeSink's `sleep`,
  `lock` and `stop` marks reach the brain only after the Mac wakes. Replayed over 10-14 to 10-23
  with the marks held back until the wake (12 sleeps of 30 minutes or more, 17.5 hours, 75
  15-minute polls), judging up to now raised an ongoing silence in 31 of those polls (17
  microphone, 12 spans, 2 screen text), none of them real.
- The nightly check is read-only and runs once a day at 06:30 local, so a break at 16:00Z is first
  seen about 21 hours later.
- Phone sources (location, motion, health, visits) have no signal for when the phone should be
  producing: their longest gaps over 10-09 to 10-22 run from 5.6 h (motion) to 40 h (location) and 156 h
  (visits), and nothing in the log says whether the phone was off, asleep or power-saving.

## Decision

The nightly check gains one invariant, `sources.keep_arriving`, and the daemon raises one card
when a source goes silent and closes it when the source arrives again.

- A source is judged by the eligible time since its last arrival, not by the clock: time counts
  only while its precondition holds, and only up to the newest TimeSink row the Mac copy holds
  (the last the brain heard from the Mac). The source is silent when that time passes an allowed
  gap, two windows of the source's own cadence. Screen text: one hour, while the Mac is awake,
  unlocked, not idle, TimeSink is running and neither tracking nor capture is paused. Spans: two
  hours, the same except that capture may be paused and a hands-off stretch (`hands_off` to
  `hands_on`, the marks TimeSink's hands-off work writes) owes none; a span's later shipped end counts as an arrival. The
  microphone (the pieces of any device in the lifelog archive, read from their file names): two
  hours, while the Mac is awake and TimeSink is running. TimeSink's `start` and `stop` clear the
  marks made before them, as the doing lane does.
- The invariant reports every silence inside the check's window by source, the time it tripped,
  the last arrival, whether it ended, and the eligible minutes; names, times and gaps only. It is
  `skipped`, with the reason, when `imports/shipped.db` is missing, and for each source that
  has had no arrival for 14 days (switched off on purpose).
- The card is a job-alert row (the spend card's shape, ADR 0185) raised by a daemon watcher that
  asks the same question about now every 15 minutes: one card per silent source per outage,
  naming it and the last arrival (or "more than 2 days" when that is older than the window),
  not made again once made, even if he settled it. Recovery closes it, so a second break is a
  second card. ADR 0185's alert routes, served only with `spend_cap` set, are served whenever
  memory.db exists.
- The window is anchored at the log's newest event. The Mac copy, a rollback-journal file a
  daemon writes, is opened `mode=ro`, never `immutable`; the time-window statements name their
  index and the state marks are read by key, as ADR 0324 requires.

## Alternatives rejected

- **The nightly CLI raises the card** — it opens everything read-only and runs once a day. Replay:
  the card would have come on 10-18 at 06:30 local, 21 hours after the OCR break began, against
  about two hours with a watcher that asks every 15 minutes. Only `runtime/` wires the judgement
  (L2) to the card store, so the watcher lives there and calls the same function the check does.
- **Two consecutive bad fixed windows** — a window that is half locked is neither good nor bad,
  and window edges move the trip time. The eligible time since the last arrival has no such
  case and is the same debounce: two windows of the cadence equal the allowed gap.
- **Two hours for screen text too** — its break would have tripped on 10-18 at 00:47Z instead of
  10-17 at 17:56Z, 6.9 hours later; its largest normal gap is 20 minutes. Spans get two hours
  because the shipper sends an open span at most hourly, so one hour could trip on a single
  long span; their largest normal gap is 29 minutes.
- **Judge up to now** — the Mac's sleep reaches the brain only at its wake, so the time before it
  reads as eligible: 31 false polls in the replay above, against none (and one poll early on a
  real outage) when the judgement stops at the last TimeSink row.
- **Raise a card whenever none is open** — a card he settles is `done`, so the next poll made it
  again for as long as the outage lasted. One card per outage, closed (not just settled) on
  recovery, makes a new outage a new card.
- **Judge only the Mac's own device (`macbook`) for the microphone** — the copy carries no device
  name for the machine whose sleep is the precondition, and ADR 0292 hands the day to the phone
  when the Mac does not record. Replay over 10-14 to 10-23 finds the same four stretches either
  way, so the union is not looser in practice.
- **Judge the phone's sources** — no precondition exists (gaps of 40 hours with no sign of why),
  so any gate would be a guess that flags a phone left at home.
- **Have the terminal report its microphone state** — a dead or asleep terminal reports nothing,
  which is the failure to catch; the missing pieces are the one evidence that survives it.

## Consequences

- If the Mac terminal dies, or TimeSink stops writing anything at all, the brain has nothing new
  to judge and these cards do not fire; a Mac gone quiet is a separate problem.
- A deliberately muted microphone looks like a silence: nothing tells the brain about the mute.
  The replay shows one such unexplained stretch (10-21 13:00 to 14:20Z, the terminal alive and
  sending other events) and two in the first day of the recorder (10-14).
- The microphone's gate is TimeSink's own `sleep`/`wake` and `stop`/`start`, so a Mac whose
  TimeSink is not running is not judged for the microphone either. `hands_off` and `hands_on`
  are written by TimeSink's hands-off work (branch b/timing-keys); until that is on main nothing carries them.
- Spans and screen text reach the brain through the shipper (ADR 0294), which sends a changed
  row at most hourly: a silence is seen up to an hour and a poll late.
- A silence stays in the nightly report for the two days of the window after it ended, and the
  unit exits 1 for each of those nights.
- A source switched off for good raises its card once and is retired from judgement after 14
  days; a recorder disabled in config is not known to the brain until then.
- `GET /inherent/notices` is now served whenever memory.db exists, not only with `spend_cap` set
  (ADR 0185 required it); clients that took its absence as "no alert rows" get an empty list.
- The card reads as a mail-kind notice with a title and one line, as the spend card does; the
  clients show nothing richer.
