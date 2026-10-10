# ADR 0199 — The brain serves each day as one timeline, folded when asked

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- Allen picked the 星盘 design for the phone app (2026-10-09). It is a dial of one day with two
  lanes:
  - 你, what he did;
  - 她, what she did or holds for him.

  The past half shows where he was and what he did. The future half shows what is coming. He
  wants that line to record his whole day, filled in over time (「慢慢补全就行」, 2026-10-09).
- The pieces of a day live on the brain:
  - the phone's places, motion, Health spans and states (ADR 0197);
  - reminders (ADR 0179);
  - conversations with her;
  - the Mac's activity, read from TimeSink through the terminal (ADR 0170).
- Two screens will draw the same day: the phone app and the Mac companion.
- The phone reports raw signals, not a day:
  - A visit arrives twice, once at arrival and again with its departure.
  - Motion comes as many short segments.
  - Sleep comes as stage spans.
- A day holds at most a few hundred phone rows. Folding them is a single indexed read of the
  log, the same kind the reminder clock does every 15 s.

## Decision

The brain answers a request for a day with that day's items. Each item is a span of time in one
lane, with a kind and structured fields. The brain folds the items from the owner's state at
each request and stores no timeline.

Its limits:

- **Structured, not worded.**
  - Items carry times, kind, place and text the owner or a source already wrote, such as a
    reminder's text or a place name.
  - Each screen words and draws them itself.
  - What she says about the day is a separate thing (the day prose of the know-you line).
- **The day is the owner's local day.**
  - The day runs midnight to midnight in the work-state timezone.
  - An item that crosses midnight is returned whole, with its real start and end.
- **One fold per source.**
  - Each source contributes its own items: phone stays, moves and sleep; reminders; later the
    Mac's work and her conversations.
  - A source that cannot be read leaves its items out, and the answer says which source was
    missing. It does not fail the day.
- **Items are only what the brain was told.** A gap in the signals is a gap in the line; the
  brain does not fill it with guesses.

## Alternatives rejected

- **Each screen builds its own day.**
  - The phone has no Mac activity and no record of her side.
  - The Mac has no phone signals.
  - Two folds of the same rows would drift apart the first time one of them changes a merge
    rule.
- **A stored timeline table, updated on every event.**
  - It is a second copy of state that can disagree with the log.
  - A late phone batch (sent at the next wake, ADR 0197) would have to rewrite past rows.
  - Folding a day at read time costs one read of a few hundred rows.
- **Return sentences instead of items.** A dial needs positions in time. Words for a span
  belong to the screen and its language, and her account of the day is a separate thing.

## Consequences

- Every merge rule lives on the brain:
  - visit arrival and departure;
  - motion gaps;
  - sleep stages.

  A screen that wants a different grouping has to change the brain.
- Each request re-reads the day. A screen that polls fast pays for it, so it should refresh on a
  push or on opening, not on a short timer.
- The Mac's part of the day is missing on a brain whenever the Mac's terminal is not connected,
  because TimeSink lives on the Mac.
