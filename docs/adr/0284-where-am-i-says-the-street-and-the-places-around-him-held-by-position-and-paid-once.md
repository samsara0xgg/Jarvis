# ADR 0284 — `where_am_i` says the street and the places around him, held by position and paid once

**Status:** Proposed
**Date:** 2026-10-15
**Supersedes:** none

## Context

- `where_am_i` (ADR 0194, 0198, 0281) returns a point, its accuracy and its age, and a place name
  when a kept place holds it (ADR 0234) or the phone gave one. It has no street, no cross street
  and no idea what is next door, so "where am I" and "what is near" end in a coordinate or in a
  second tool call.
- He wants to open Google Maps rarely (ADR 0283). What Maps shows at a spot is a street, a cross
  street and the places round it; which of them he is *at* is a judgement from how long he has
  stood there, what he keeps and where he has been, which the program holds (ADR 0197, 0234).
- A phone that stays put sends nothing, so the spot is the same for hours and a lookup per
  question would pay Google again for the same answer. Every request costs money against the
  monthly cap (ADR 0283).
- Places API (New) has no reverse geocoding and no intersection. The Geocoding API has both
  (`result_type=intersection`), at the Essentials tier. The key's project had Geocoding off when
  ADR 0237 probed it, and Places may be off as well.
- The state block is sent on every turn and its changing lines sit after the cached prefix
  (ADR 0148, 0249). A request made while building it would make every turn wait on Google and
  pay for it unasked.

## Decision

`where_am_i` also returns `around`: the street and nearest intersection, the places near him and
the facts that say where he probably is. The model judges; the program returns facts only.

- **Street**: one Geocoding API reverse request filtered to `street_address|intersection` gives
  `address`, `street` and `intersection`.
- **Places**: one Nearby Search with no type filter, radius 300 m, 20 results by distance, field
  mask `AROUND_MASK` (id, name, address, point, primary type name, `currentOpeningHours.openNow`,
  so billed as Nearby Search Enterprise). Those within `max(accuracy, 20) + 30` m, at most 5, are
  `candidates` (with address, place_id and `past_stays`, the stays in the last 84 days within
  60 m of the place); the next 10 within 300 m are `nearby`. Each has kind, distance, compass
  direction and open now.
- **Facts from what he has**: `here_for_min` (the unbroken run of the phone's location fixes
  within a spot's radius, and an open visit if earlier, looked back 6 hours); `kept_place` with
  its counts when he is inside one, else `unkept_spot` with its counts. When he confirms a place
  the note tells the model to keep it with `save_place(where="here")`; no tool is added.
- **Held by position**: a reading answers any point within 50 m of the one it was made at, for
  3 hours, in memory (32 readings); a restart empties it. A part that failed is not held and the next
  call tries it again; a part that came is not asked for twice. Distances are measured from the
  current fix to the held places.
- **Cost**: the reverse request is `geocoding.reverse`, 0.005 USD, and the search
  `places.nearby_search.enterprise`, 0.035 USD, recorded and guarded as ADR 0283 does. Near the cap
  or with Places or Geocoding off (a 403, `places_not_enabled`) or no key, `where_am_i` still
  returns the fix, the kept place and the stay length, and `around.unavailable` holds each reason.
- **State line**: the block gains `Around him: <intersection or address>; at <kept place>` or
  `maybe at <nearest place>` (only after ten minutes within a spot's radius), under 160 characters,
  from the phone's latest report if it is under 3 hours old and not a departure. It reads the held
  reading for that position and never makes a request; with none it shows the kept place alone, or
  no line. It carries no stay length, so it changes only when he moves to another cell or gets
  somewhere.

## Alternatives rejected

- **Places only, street from the nearest place's address**: gives a street but no cross street,
  which is what tells a corner from a block; Geocoding costs 0.005 USD once per spot.
- **A request for the state line when nothing is held**: 0.04 USD for each new cell, on a turn
  nobody asked about it, and the turn waits on Google.
- **Candidates ranked by a score of stay, visits and distance**: the weights are a judgement the
  model makes better with the question in hand; the program orders by distance and returns each
  fact beside it.
- **A cell grid of 50 m**: two points 5 m apart across a cell edge pay twice; the nearest held
  point within 50 m has no edge.
- **Persist the held readings**: a restart costs one 0.04 USD lookup per spot; a table and its
  expiry are more than that.

## Consequences

- `open_now` is up to 3 hours old when read from a held reading; `looked_up` says how old.
- The price of Geocoding is from memory of the list price (5 USD per thousand), without the free
  allowance, so the count runs high like ADR 0283's; the key travels in the Geocoding URL, as in
  ADR 0237.
- A held reading answers any point within 50 m of its first point, so it can drift: points 50 m
  apart in a chain each match a different held reading, not one.
- The Geocoding API has to be enabled on the key's project; until it is, `around` has no street
  and says so on each call, without charge.
- `read_stays` runs over 84 days of visit rows on each `where_am_i`; the state line reads one
  day, and only when a reading is held and he is not in a kept place.
