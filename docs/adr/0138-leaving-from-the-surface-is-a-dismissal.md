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
voice session's `dismiss()`, which runs the stop, the drop and the goodbye
callbacks a spoken dismissal runs, with no words to take a language from.

Its limits:

- Only the on-to-off edge through this route: voice's own flips (a dismissal,
  quiet) set the switch directly and do not come back here, and an exit with
  the mode already off does nothing.
- The goodbye is the ADR 0102 line in the reply-language setting, or the
  system language when it follows his words and there are none.
- `cancel-response` is unchanged.

## Alternatives rejected

- **`foreground_output` for a named response also stops the audible one** —
  `test_stop_foreground_output_interrupts_only_the_named_response` pins that a
  named stop touches only its target, and a per-answer stop on the surface
  would cut an unrelated answer.
- **A second stop path in the controls route** — the route would need the
  player, the run registry and the line bank; the session already holds the
  callbacks, and two paths drift apart: the dismissal gained the drop of an
  answer on its way on 2026-10-02, and a parallel exit would have missed it.

## Consequences

- The exit shares a dismissal's reach: an answer dropped is one of a voice
  sentence or ask card heard in the last 10 s (ADR 0074), plus the single
  open final run; two open runs older than that are not cancelled.
- A text-only daemon has no session, so the exit only flips the switch.
