# ADR 0229 — She picks the day's highlights and tells him the day in one line

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

The day ring draws every item of the day at the same weight. On 2026-10-11 he
asked for a ring that "really describes my whole day" and its highlights, and
agreed that she picks them and he can change them.

- "The day" prose (ADR 0201) is the wrong text to show him. It is English,
  2 to 4 sentences, third person, written only for complete past days, and its
  reader is the prompt. Changing it changes what every turn sees.
- A highlight's label needs words the items do not hold as a short phrase: the
  dish in a picture note, or why a call mattered. Rules can rank by duration or
  novelty but cannot name the dish.
- The phone holds no model key. Highlights must look the same on every device.
- Cost: a day-prose call on `gpt6-luna-flex` costs $0.0002 to $0.0004
  (Pi cost.recorded, 2026-10). A pass every 30 minutes over a waking day is
  about 30 calls, around $0.01 a day.
- ADR 0199: nothing invented. A highlight must point at an item that exists.

## Decision

While a day is under way, and once after it ends, she reads that day's day line
and writes a told-day: one line to him in his language, and up to five
highlights, each an existing item id with a short label. It is stored as one
event per day, and the latest event wins. His own marking or unmarking of an
item through her wins over hers. The day line serves the line and the
highlights with the day. Told-days are kept for good. The nightly memory
passes read the final told-day as part of that day's input: the day summary
(ADR 0142), the core memory (ADR 0146) and "The day" (ADR 0201). Nothing is
copied into memory.db, so a correction reaches every reader from its next
read.

Limits:
- A pass runs only when the day has changed since the last one, and at most
  once per 30 minutes. A final pass runs after midnight, before
  the nightly memory passes.
- An answer whose ids are not in the day, or whose line or labels are over
  length, is dropped whole. The previous told-day stays.
- ADR 0201's prose stays its own text for the prompt. The two texts have
  different readers.

## Alternatives rejected

- **Rules only** (duration, first visit, rides, pictures) — they can rank but
  cannot label. A label built from fields reads "picture 18:52" instead of
  what was in it. Rules still order the input so the model sees the big items
  first.
- **Rewrite ADR 0201's prose for the ring** — it is English and past-only, and
  the prompt's ledger depends on it. One text serving both readers would make
  each worse.
- **Model on the phone** — no key there, and two devices would disagree about
  the same day.
- **Pass on every new item** — work and talk items change every few minutes.
  That is hundreds of calls a day for a line he reads a few times.

## Consequences

- The line can lag the day by up to 30 minutes.
- A wrong highlight stands until the next pass or until he corrects it.
- A model call is added to the brain's background work, so it can fail.
  Failures leave the previous told-day in place.
