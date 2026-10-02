# ADR 0099 — Explicit Spoken Requests Override Summary Limits

**Status:** Superseded-by-0116
**Date:** 2026-09-28
**Supersedes:** 0045

## Context

- A separate spoken-form request limits ordinary replies to 60 Chinese
  characters or 40 English words. The full answer remains on screen.
- During the Mac voice test, counting from one to fifty was fully present
  in the document but reduced to “one to fifty” in the spoken form. A
  second attempt spoke only an acknowledgement of the request.
- Allen asks to retain the short default unless the user explicitly
  requests otherwise. Counting and reading aloud require performing the
  requested speech, not summarizing its result.

## Decision

Keep the short spoken form as the default, and let an explicit request for
counting, reading aloud, verbatim repetition, detail, or a specified length
override the summary and length limits while preserving the requested
content and order. Remove visual markup without replacing the requested
speech with an acknowledgement. Retain ADR 0045's rewrite trigger,
language selection, exclusions, full-document retention, whole-answer
fallback, and disabled routine commentary.

## Alternatives rejected

- **Always summarize to 60 characters.** A fifty-number answer cannot be
  performed faithfully under the fixed length and one-or-two-numbers rule.
- **Always read every answer in full.** Ordinary weather and search replies
  would again read long written answers even without a request for detail.

## Consequences

- Explicitly requested speech can be long; stopping playback remains the
  user's way to end it early.
- Intent is interpreted in the existing spoken-form request, so a live
  model check remains necessary for prompt changes.
