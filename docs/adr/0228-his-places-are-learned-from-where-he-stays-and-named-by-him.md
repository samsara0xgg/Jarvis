# ADR 0228 — His places are learned from where he stays and named by him

**Status:** Superseded-by-0234
**Date:** 2026-10-11
**Supersedes:** none

## Context

The phone's day ring (ADR 0199) colours each stretch of his day by where he was
and writes the place's name beside it. On 2026-10-11 he agreed that "where" is
one of the three things the ring must show at a glance.

- A `stay` item carries only the phone's reverse-geocoded string. The same
  building yields different strings on different visits, and a home yields a
  street address, not "home". Strings cannot give a stable identity, so they
  cannot give a stable colour.
- Home and school exist only as the `transit.home` and `transit.school`
  settings (ADR 0188/0189). Nothing matches them against stays. ADR 0227 gives
  every setting one owner, so a second copy of home elsewhere would drift.
- An iOS visit's centre wanders by tens of metres between visits to the same
  building. A campus spans several hundred metres.
- ADR 0199: gaps are not guessed. A name the brain cannot back with his word
  or a repeated observation is a guess.
- The brain owns state. A second phone or the Mac must see the same places.

## Decision

A named place is event-log state on the brain: a centre, a radius and a name
he gave (through her, `place.named`; renamed by a later event; removed by
`place.forgotten`), plus `transit.home` and `transit.school` read as the places
home and school without copying them. Each `stay` the day line serves carries
the named place whose circle holds it: id, name, and role home, school or
other. A stay inside no named place keeps only the phone's string. Everything else that speaks of
where he was reads the same places: the ledger (ADR 0201), recall, and the
transit and ride tools. Nothing copies a name elsewhere.

Limits:
- Places are never named by a model or a geocoder. A frequent unnamed spot is
  something she may ask him about. It does not become a name.
- When circles overlap, the smallest one that holds the stay wins, so a café
  he named on campus beats "school".

## Alternatives rejected

- **Name from the geocoder's string** — two visits to the same campus building
  return different strings. This is reproducible from any week of his `stay`
  items, so the colour would flicker between days.
- **Cluster stays and name clusters automatically** — the name has to come
  from somewhere. A model's guess at a name breaks ADR 0199 the first
  time it is wrong, and he cannot tell a guess from a fact on the ring.
- **Keep places on the phone** — the Mac and a future second phone would not
  see them, and her tools on the brain (transit, rides, recall) could not use
  them.
- **Copy home and school into place events** — two owners of one setting. ADR
  0227 exists because such copies drifted.

## Consequences

- A place exists only after he names it once ("这里是我家", or an address). Until
  then that stretch shows the phone's string.
- A badly centred place mislabels nearby stays until he renames or resizes it.
- The event log now holds coordinates he chose to name. They already sit in
  phone events, so this adds no new kind of data.
- The public repo must keep real coordinates out of fixtures and tests.
