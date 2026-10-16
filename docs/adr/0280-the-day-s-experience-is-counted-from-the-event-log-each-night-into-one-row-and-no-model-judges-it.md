# ADR 0280 — The day's experience is counted from the event log each night into one row, and no model judges it

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** none

## Context

- ADR 0275 makes the owner's day the product. Whether a day went well is today answered by
  hand: an investigator reads the log for latencies, repeats and dismissals, once. Nothing keeps
  the answer per day, so a change cannot be compared with the week before it.
- Almost every fact the answer needs is already on the event log: the turn that opened (spoken
  `utterance.received`, typed `surface.user_intent`, with the device that wrote it), the guards,
  failures, cancels and dropped speech that follow it, the first sound or first text, playbacks
  and their interruptions, reminders fired late, rides ended past a stop.
- Two facts are not. Jev's one request per voice line (ADR 0139) answers the line's intent, its
  relation to the line before and the tool group, but its answers reach only the Jev dataset file
  (ADR 0128), keyed by turn; the log carries the intent only inside `route.surrogate_decided`,
  and only for lines that reached the decision stage. That file is off with `jev_log.enabled:
  false`, holds the words he said, and is not a store a fold should read.
- A turn that a later sentence took over is cancelled as `superseded` with the turn that took
  its place named (ADR 0268, 0271); counted as a use it would count the same sentence twice.
- Precedent for derived per-day rows: `day_summaries` in memory.db, filled after a configured
  local time for every past date without a row (ADR 0142). Settings have one owner (ADR 0227).
- `reminder.fired` records how it was delivered (`speak` or `card_sound`), not whether it was
  heard. `notice_feedback` records a card shown and left as `ignored` only when the next card is
  shown (ADR 0160).

## Decision

Fold each past local date of the event log into one `day_experience` row of memory.db every
night, from logged facts alone and with no model call, and have the Jev one-shot write its
verdict per voice line as a log event so the fold can read it.

- **What a row holds.** Real uses by device and by spoken or typed (a card tap, an ask-card
  answer, a plugin resume, a Live delegation and a superseded turn are none); the share of uses
  with no redo signal, naming each signal that counted (a guard, a failure, a stop or barge-in
  cancel, dropped speech, an ask card, and a following line within 120 s that Jev read as a
  correction of it); median and 90th percentile of voice latency (end of his speech to first
  sound, only where the device reported when he stopped) and typed latency; the share of
  playbacks interrupted; answers given to lines Jev read as unrelated; claims the unbacked guard
  caught; proactive outcomes followed, dismissed and ignored; reminders more than five minutes
  late and rides past their stop unwarned; a label per voice turn.
- **Deliberately null.** `plan_settled` and `calendar_missed` are stored as null: the planning
  feature of ADR 0275 will own them.
- **Where it is thin, it says so by under-counting.** The unrelated reading exists only for a
  line that had an earlier line inside the relation window; a reminder counts as missed for
  lateness, not for non-delivery, because the log has no such fact; `ignored` lags until the
  next card. A correction of a turn that a later turn took over is credited to the turn that
  remains.
- **Run.** The brain runs it after `day_experience.at` (default 04:00), backfills every past
  date without a row from the first turn on the log, never writes today, and does its reading in
  a thread. A date that fails stores nothing and is retried the next night.
- **Read.** `python -m jarvis.runtime.experience_report [--date] [--week]` prints a stored day
  and the week's most used and most failed label; read-only, no network.
- **Out of scope**, later slices: labelling typed turns with a cheap model, a phone's
  end-of-speech time, per-turn fact-age stamps, a test-turn flag.

## Alternatives rejected

- **Read the Jev dataset file for the labels.** It holds the words he said and is skipped when
  `jev_log` is off, so the table's labels would vanish with a logging switch. The new event
  carries labels and confidences only and lives where every other fold reads.
- **Let a model judge each turn ("did that help?").** One call per turn, and a verdict nobody can
  re-derive. Every signal chosen is a logged fact: re-running the fold on the same log gives the
  same row, which is what lets a change be compared with last week.
- **Fold on request, store nothing.** The report on another machine would need the brain's event
  log open and a day of events decoded per row shown; the weekly line would decode seven. A
  stored row is one small read, and the log stays append-only.

## Consequences

- One more event per voice line on the log, written only when Jev's reply was readable.
- A rule changed later (the 120 s window, the five-minute bound, what a use is) leaves older rows
  on the old rule, because a date with a row is never refolded; a rule change must delete the rows
  it wants refolded.
- Days before this lands have no labels, no correction signal and no unrelated count, and a row
  for a day with no activity is all zeros: a week of rows compares days, not only busy days.
- The table says how a day went, not why; reading a bad number still means opening the log.
