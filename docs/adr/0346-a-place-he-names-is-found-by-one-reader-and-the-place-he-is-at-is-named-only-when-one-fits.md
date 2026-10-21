# ADR 0346 — A place he names is found by one reader, and the place he is at is named only when one fits

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0284, the state line's `maybe at <nearest place>`

## Context

- Each tool reads his words for a place its own way:
  - `bus` knows here, home, school and kept names. Any other words go to Google Routes as plain
    text, with no bias to where he is. On 2026-10-14 "the nearest A&W" was sent that way and Google
    chose a branch; ADR 0283 left choosing the branch to the model.
  - The ride knows home and school, then the label his phone gave a past stay.
  - `map_search` adds an area's words to the search text and never finds the area itself.
  - `save_place` takes here, a spot id or a kept place id, but no address and no place found on
    the map, although a kept place can hold an address (ADR 0237).
  Each copy drifts from the others, as the copies of "here" did before ADR 0322.
- His words are often a business or a chain ("A&W", "Shoppers"). Which branch he means is almost
  always the nearest one, and Google's Text Search can rank by distance from where he is.
- A Text Search that asks only for the name, address and point is billed at the Pro tier, inside
  the free monthly usage at his volume (ADR 0347).
- `save_place` changes his memory but returns no undo id, although `places.undo` exists. The spec
  asks every write to return one (`#tools`).
- The state line says `maybe at <nearest place>` after ten minutes at a spot, whatever else is
  near. On 2026-10-14 in a restaurant the nearest place was not the one he was in; in a plaza five
  shops can lie inside one fix's accuracy.

## Decision

Turn his words for a place into a point through one reader, used by every tool that takes a place;
and name the place he is at only when one place fits.

- **The reader.** It answers with a point, the place's bounds when it has them, a name and address
  to say back, and how it found the place. It tries, in order:
  - here, which is the call's Here;
  - a place he keeps, by its name in any case, home and school among them;
  - a place id from a `map_search` result;
  - otherwise one Google Text Search near Here, ranked by distance. A chain's name gives its nearest
    branch.
- **Its users.** `bus` for where to and where from, the ride for where he is going and where he gets
  off (through the stop finder, ADR 0344), `map_search` for an area (ADR 0345), and `save_place`.
- **Keeping a place.** `save_place` takes here (from a fresh Here), a spot or kept place from
  `places`, or any words the reader finds, such as an address or a place from `map_search`. It and
  `forget_place` give the change id as the undo id in their receipts (ADR 0319).
- **The place he is at.** The state line names a place he may be at only when exactly one place
  lies within his fix's reach and he has stayed ten minutes. When several do, it names up to three
  as the places he may be at. `where_am_i` still returns all of them, as facts.

## Alternatives rejected

- **Let Google Routes read the words inside the route request (today).** It is free, but Routes
  takes no bias to where he is, so "the nearest A&W" can route to any branch, and the answer cannot
  name the branch until after routing.
- **One reader inside `bus` only.** The ride, the area of a map search and `save_place` would keep
  their own readings, and the next copy would drift as `map_search`'s copy of `bus`'s "here" did
  (ADR 0322).
- **Name the nearest place whenever he has stayed ten minutes (today).** In a plaza the nearest of
  five places inside the fix's accuracy is a guess. Said as the one place, it is a guess that looks
  like a fact.

## Consequences

- A destination in words costs one Text Search before the route is planned.
- Vague words ("the mall") become the nearest match. He hears its name and address, and must
  correct a wrong one.
- A kept place's name hides a business of the same name; he has to say more to reach the business.
- In a busy place the state line names no single place, so finding out which one takes a question
  to him.
