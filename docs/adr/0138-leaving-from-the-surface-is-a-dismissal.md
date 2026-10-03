# ADR 0138 — Leaving From the Surface Is a Dismissal

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- Allen, 2026-10-03: when he tells her to go back, she must stop talking at
  once and then say she is going back.
- The companion's exit sends `POST /inherent/cancel-response` for the last
  answer it opened (scope `foreground_output`) and then `POST
  /inherent/controls` with `conversation: false`. Observed 10:22-10:23 PDT:
  the named answer was queued, not audible, so only it was dropped
  (`_stop_foreground_output_owned`, ADR 0008 D10), and an older answer kept
  talking 12 s after the exit. The controls route only flips a boolean.
- A spoken dismissal (ADR 0102) already does it right: it stops what is
  audible, drops an answer still on its way (ADR 0053, 0074) and says one
  fixed goodbye line. That path runs in the capture session, which the
  controls route cannot reach on its own.
- `cancel-response` with a named response is tested and documented as
  touching only that response; other callers rely on it.

## Decision

`POST /inherent/controls` taking conversation mode from on to off calls the
voice session's `dismiss()`: every open answer run that would speak is
cancelled, what is audible stops, and she says the ADR 0102 goodbye line.

Its limits:

- Only the on-to-off edge through this route: voice's own flips (a dismissal,
  quiet) set the switch directly and do not come back here, and an exit with
  the mode already off does nothing.
- A run is cancelled whatever its age or number if it is open, is not for a
  document alone, its policy lets its generation be cancelled, and its turn
  started from Allen's words on a channel the speaker is not silent for
  (`_TTS_SILENT_CHANNELS`). Its turn is marked stopped, so a run of it
  opening later is cancelled at its open. Background turns are left alone.
- With no words the goodbye takes the language of what he last said or
  typed, the system language when nothing was heard; `reply_language` still
  pins it.
- The spoken 退下 path is unchanged, and `cancel-response` is unchanged.

## Alternatives rejected

- **`foreground_output` for a named response also stops the audible one** —
  `test_stop_foreground_output_interrupts_only_the_named_response` pins that a
  named stop touches only its target, and a per-answer stop on the surface
  would cut an unrelated answer.
- **A second stop path in the controls route** — the route would need the
  player and the line bank; the session already holds both callbacks.
- **The spoken dismissal's drop as it is** — it reaches only voice turns
  heard in the last 10 s and the single open final run, so a run still being
  written for an older turn, or one of two, would speak after the goodbye.

## Consequences

- The exit reaches further than a spoken dismissal, which drops only
  unspoken answers of voice sentences or ask cards heard in the last 10 s
  (ADR 0074) and the single open final run.
- A turn that has started but not yet opened a run is not named: only open
  runs are found, so a run opening in that gap is not cancelled.
- A text-only daemon has no session, so the exit only flips the switch.
