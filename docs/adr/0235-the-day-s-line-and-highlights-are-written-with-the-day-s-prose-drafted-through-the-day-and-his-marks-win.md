# ADR 0235 — The day's line and highlights are written with the day's prose, drafted through the day, and his marks win

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** 0229

## Context

The day ring draws every item of his day at the same weight. On 2026-10-11 he
asked for a ring that really describes his whole day. He agreed that she
picks up to five highlights and writes him one line, and that he can change
the highlights. At 19:12Z he set the rule that judgement belongs to the model,
used through well-described tools.

The memory thread, which owns memory.db, ruled that the line and highlights
must not become a third daily text. A day already has two:

- a summary, which recall returns (ADR 0142);
- "The day" prose, which the ledger shows (ADR 0201).

Constraints:

- "The day" prose is English, 2 to 4 sentences, written nightly for complete
  days only, and its reader is the prompt. Folding it into one short line
  would take context away from every turn.
- A highlight's label needs words that only a model can write, such as the
  dish in a picture note (ADR 0223).
- ADR 0142 never summarises today, but the ring shows today while it happens.
- A `gpt6-luna-flex` call for a day's prose costs $0.0002 to $0.0004 on the
  Pi. He accepted about 1 US cent a day for drafts.
- ADR 0199 forbids invention. A highlight must point at an item that exists.

## Decision

The night's prose call for a day also writes that day's line and highlights,
in the same `day_prose` row. While a day is under way, a draft row holds the
line and highlights alone, and the night's row replaces it.

- **The line** is one sentence to him, in his language.
- **Highlights** are up to five ids of that day's day-line items, each with a
  short label. An answer naming an id outside the day, or over length, is
  dropped whole, and the previous row stands.
- **Drafts** run when the day line has changed since the last draft, at most
  once every 30 minutes.
- **His marks** come through a tool (`mark_highlight`). They are stored as
  their own rows and always override her picks.
- **Readers.**
  - The day line serves the line and the merged highlights with the day.
  - Recall returns them with the day's summary.
  - The ledger's "The day" text is unchanged.

## Alternatives rejected

- **A separate told-day event (ADR 0229)**: it is a third daily text. It also
  lives outside memory.db, where the memory page and the nightly passes do
  not read, which is reproducible by searching recall for a told-day line.
- **Rewrite "The day" into the line for both readers**: the prompt would
  lose 2 to 4 sentences of context per day to serve a line written to him.
- **Rules alone pick highlights** (duration, first visit, pictures): rules
  can rank but cannot label. A picture would read "picture 18:52", not what
  was in it.
- **Write only at night**: today's ring would stay without a line or
  highlights until the next morning.

## Consequences

- The line can lag the day by up to 30 minutes.
- A failed draft leaves the previous one in place. A wrong highlight stands
  until the next draft, or until he marks it.
- The night's prose call writes more output, and the drafts add about one
  cent a day.
