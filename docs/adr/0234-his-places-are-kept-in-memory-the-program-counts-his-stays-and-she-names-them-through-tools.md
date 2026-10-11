# ADR 0234 — His places are kept in memory: the program counts his stays, she names them through tools

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** 0228, 0233

## Context

The phone's day ring (ADR 0199) colours each stretch of his day by where he
was and writes the place's name beside it. On 2026-10-11 he set three
conditions.

- Places he goes to often belong in his long-term memory. Places he visits
  now and then (a restaurant once a month) do not, but how often he goes is
  still worth knowing.
- A place can enter memory in two ways: he says it ("我经常来这儿"), or she
  notices it.
- At 19:12Z he said the best design is better tools whose use is well
  described, with the model judging, not quantities fixed in code.

The memory thread, which owns memory.db, ruled that places are their own
memory.db table, versioned and undoable like core memory (ADR 0154). They are
not core-memory items and not event-log state.

Constraints:

- A `stay` carries the phone's latitude, longitude, accuracy and one
  reverse-geocoded string. Two visits to the same building return different
  strings, and a home returns a street address. A string cannot be a place's
  identity. An iOS visit's centre wanders by tens of metres between visits;
  a campus spans hundreds.
- A model cannot count a month of stays reliably. It also cannot remember,
  from one day to the next, whether it already asked about a spot.
- Home and school exist only as the `transit.home` and `transit.school`
  settings (ADR 0188/0189), read once at boot. School may be an address with
  no coordinates, and the repository has no geocoder.
- ADR 0146 (as amended in conversation on 2026-10-11): memory is written in
  conversation only when he asks for it.

## Decision

A place he keeps is a row in a memory.db table, written only through
versioned, undoable changes. Each row holds a name he gave, a role (home,
school or other), and a fixed centre and radius, or an address alone until
he is there. The program derives the facts from his stays. She decides,
through tools, what is kept, under which name, and when to ask.

- **Facts.** Stays within 100 m of each other form a spot. For every spot and
  every kept place the program derives: the days visited in the last 4 and
  12 weeks, the last visit, and the usual length of a stay. Spots are
  recomputed on read and have no identity. A kept place's circle does not
  move.
- **Tools.**
  - `places` lists the kept places and the most visited unkept spots, with
    their facts and the phone's label for each spot.
  - `save_place` keeps or renames a place at "here", at a listed spot or at a
    kept place.
  - `forget_place` removes one; its visits stay counted.
  - The tool descriptions tell her when each applies, including keeping a
    place on her own judgement, but only under a name he has used.
- **Asking once.** While he is at an unkept spot visited on 3 or more days in
  the last 4 weeks, the turn's state block shows its facts once. Whether to
  ask is hers. The program records that the line was shown, so it never
  comes back for that spot.
- **What reads it.**
  - A `[Places he keeps]` block follows `[About the user]`, one line per
    place with its rate.
  - The ledger names a stay by its place.
  - The day line gives each stay the place whose circle holds it; the
    smallest circle wins.
  - Transit and ride tools read home and school from this table.
- **Home and school** move from settings into the table once, at the first
  boot that has the table. The table is their only owner from then on.

## Alternatives rejected

- **Event-log state (ADR 0228, 0233)**: the memory page, its versions and
  undo (ADR 0154), and the nightly memory passes read only memory.db. Places
  in the event log are invisible to all of them, which is reproducible by
  opening the memory page after any `place.named` event.
- **A fixed count decides what is kept (ADR 0233's 4 days in 28)**: he
  rejected it on 2026-10-11. A count cannot tell a boxing gym visited twice
  a week, which matters to him, from a shop he walks past daily, which does
  not. The 3-days figure above only decides when a fact is shown to her.
- **Places as core-memory items**: an item is prose. A centre and radius
  would be parsed back out of text, and the nightly consolidation rewrites
  items.
- **Ask by push as soon as a spot qualifies**: he is often not there at
  that moment, and the question arrives with no context.

## Consequences

- She can keep a wrong place from a misheard sentence. Undo exists for it,
  but nothing catches it on its own.
- Transit tools read places at call time instead of boot settings. A place
  with only an address routes but cannot match stays until he names it on
  the spot.
- One more prompt block. Any change to a place costs one prompt-cache miss.
- Repository fixtures must hold no real coordinates.
