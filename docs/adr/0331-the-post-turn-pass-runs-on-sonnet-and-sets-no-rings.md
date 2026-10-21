# ADR 0331 — The post-turn pass runs on Sonnet and sets no rings

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** the clauses of ADR 0329 that have the pass read whether he asked to be told (`remind`) and make rings from it, and the clause of ADR 0300 that runs the pass on `haiku-bg`. Everything else in ADR 0300, 0328 and 0329 stands.

## Context

- 2026-10-21 the pass replayed 263 records of his (10-13..10-19) on the Pi's copies, once with
  `haiku-bg` and once with `sonnet` (Sonnet 5.5, low effort), on the same code.
- The 10-18 00:41 screenshot of his passed written test should close the open 考 ICBC 笔试.
  Sonnet closed it; Haiku closed it in one of three reads (the 0329 replay, the live re-read
  and this replay). Sonnet also wrote three things he said he would fix that Haiku did not.
- Sonnet answered one record with no JSON, which was marked an error and wrote nothing.
- Cost, measured: Sonnet 1.11M tokens in and 4.3k out, about $2.26 for the week; Haiku about
  $0.13. At about 33 records a day Sonnet is about $0.28 a day.
- Both models set `remind` on two records where she had offered to remind him and he had not
  asked (10-17 10:14; the 10-18 00:39 picture, where she then set the reminder herself on his
  yes). On none of the 263 records did he ask to be told and she leave it unset.
- 2026-10-21 yilun asked whether Haiku is enough and approved the comparison.

## Decision

The pass runs on `sonnet` and never makes rings; telling him is hers to set in the turn.

- `turn_things.preset` ships as `sonnet`; the replay script defaults to it.
- The model is not asked whether he wants to be told. A `remind` key in an answer is not read.
  A thing the pass adds has no rings.
- `private` stays: a ring she sets for a private thing says only its time.

## Alternatives rejected

- **Stay on Haiku** — it missed the one "done" the week turned on, two times out of three, and
  the difference is about $0.27 a day.
- **Keep `remind`, backed by a line he must cite** — the model would cite the line it misread;
  code cannot check that a line asks to be told, and the week shows no case it would have caught.
- **Keep `remind` with the same-minute check against her reminders** — it caught the 00:39 yes
  but not the picture record read before her reminder existed, and not an offer he never took.

## Consequences

- About $0.27 a day more than Haiku at this week's volume.
- When he asks to be reminded and she misses it, nothing rings until he says it again; the thing
  is still in the ledger and in the state block.
- The extractor version changes with the instructions.
