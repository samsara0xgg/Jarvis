# ADR 0126 — A speech candidate ends at the first clause within 240 code points

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- ADR 0008 D5 gives speech candidates an initial maximum of 60 Unicode code
  points. 60 code points is about 1 s of English speech (60 CJK characters
  are about 13 s of Chinese); spec v1 already used 60 CJK characters and 240
  English characters as the spoken-form limit.
- Voice harness run, 2026-10-02: the answer "A transformer reads text as
  tokens, then uses attention to decide which other tokens matter for
  understanding each one. It processes those relationships ..." streamed one
  chunk, "A transformer reads text as tokens,", and everything else arrived
  with `done`. After the first clause the buffer had no clause or sentence end
  within 60 code points, so the assembler set `no_safe_bounded_boundary` and
  the rest of the run was held for full-text handling.
- A boundary is still the only safe cut: protected dots, numeric commas,
  unbalanced prose and unsupported syntax must not be split, and an arbitrary
  offset never goes to TTS.
- The stream gate rejects a speech chunk longer than the bound with
  `buffer_full_text`, so the assembler and the gate must share one number.

## Decision

A speech candidate ends at a clause or sentence end within 60 code points as
before; with none inside 60, it ends at the first clause or sentence end up to
240 code points; the assembler blocks with `no_safe_bounded_boundary` only
when the buffer passes 240 with no such boundary. This amends the speech bound
in ADR 0008 D5; document candidates, `unsupported_speech_syntax` and
`buffer_limit` are unchanged, and the stream gate admits speech chunks up to
240.

## Alternatives rejected

- **Keep 60 as the hard bound.** The measured answer above is 233 code points
  of ordinary prose with a 36-code-point first clause; 197 code points of it
  were held until `done`.
- **Raise the bound to 240 for every candidate.** A buffer with a clause end
  at 40 would then wait for a sentence end 150 code points later; text with a
  boundary within 60 must split exactly as before.
- **Cut at a word boundary at 60.** It sends an arbitrary half-clause to TTS,
  which D5 forbids, and it cuts through numbers and abbreviations the
  protected-dot and numeric-comma rules exist for.

## Consequences

- A long clause with no punctuation speaks as one chunk of up to 240 code
  points (about 15 s of English), so a barge-in or a gate refusal lands on a
  larger unit than before.
- An unpunctuated tail between 61 and 240 code points is now flushed as a
  final chunk instead of being held for full-text handling.
- 240 is a hand-kept number from the spec v1 spoken-form limit, not a
  measurement of the owner's answers.
