# ADR 0328 — The post-turn pass reads only what a turn changes in his things

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** the clauses of ADR 0300 that make the pass return stated doings, check them and write them as `doings.stated` sets. Everything else in ADR 0300 stands.

## Context

- ADR 0300's pass answers two questions per record of his: what it changes in the ledger, and
  what he says he did or is doing. The second was for the day's spans; on 2026-10-19 the lifelog
  layer-two thread agreed to take it from the pass rather than read his words twice.
- Nothing reads it. `jarvis/runtime/turn_things.py` is the only writer of `doings.stated` and no
  module calls `stated_doings.standing`; the doing writer (ADR 0302, 0310) reads the day's
  conversation itself as `doing_clues` words.
- The first backfill on the Pi (2026-10-20, 256 records) wrote 21 doing sets and 7 of its 8 drops
  were doings: most of the drops and part of every answer, for output no reader takes.
- 2026-10-21 yilun called the step 「非常非常的蠢」 next to giving the Opus day writer the day's
  whole conversation, and said 「ok」 to removing it; layer two now gives its writer the whole
  conversation.
- A past doing in the old instructions was also what closed an open thing he finished (the
  10-18 00:41 screenshot of a passed test); that rule must survive as a ledger action.

## Decision

The post-turn pass asks only what a record changes in the list of his things, and writes only
ledger actions.

- The answer is `{"actions": [...]}`; a doings key in an answer is not read.
- "done" covers his telling or showing what he did when that finishes an open thing.
- `doings.stated` is no longer written. Its event type stays registered so the sets already in a
  log still read; nothing reads them.

## Alternatives rejected

- **Keep the doings and have the doing writer read them** — the writer already reads the same
  words with the whole day around them; a second reading per record by Haiku adds a call's worth
  of output and a second account that can disagree with the first about one sentence.
- **Keep writing them for a later reader** — no reader is planned, and the 2026-10-20 backfill
  shows them as 7 of the pass's 8 drops.

## Consequences

- What he says he did is read by the day writer on its runs (ADR 0310: every two hours by day,
  the whole day at night), not minutes after he says it; a reader that needs it sooner reads the
  conversation.
- The extractor version changes, so records read again (a replay) are compared across versions.
