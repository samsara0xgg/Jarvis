# ADR 0283 — Places are searched by `map_search`, priced per request and stopped near a monthly cap

**Status:** Proposed
**Date:** 2026-10-15
**Supersedes:** none

## Context

- Allen wants to open Google Maps rarely: what is near, whether it is open, how far, a phone
  number, a review. He asked for it plain, "能用 别太复杂太乱". The brain has the same Google key
  for Routes (ADR 0189) and his phone's last report (ADR 0198).
- Places API (New) bills by the SKU of the fields asked for, not by the endpoint: rating, price
  level, opening hours and phone are Enterprise fields, reviews are Enterprise + Atmosphere, and
  a name and address by id are Pro. Prices are per request and Google bills in Pacific time.
- Routes requests already cost money: one `bus` search sends three (bus, drive, walk, ADR 0205),
  and a retry with the city (ADR 0189) a fourth. Nothing counts them.
- The key's project had only Routes enabled when ADR 0237 probed it. Places may not be enabled
  either; Google answers a disabled API with HTTP 403 and reason `SERVICE_DISABLED`.
- Routes answers a text destination with a `placeId` in `geocodingResults`, not a name or an
  address, so "the bus to Starbucks" is today said back as the words he used and a wrong match
  is invisible to him.
- A name he says can be misheard or vague. Asking Google costs one request; guessing a place to
  send him to costs a trip.
- The phone's map card (a separate change) draws places; it must get them with the answer, in a
  shape fixed before both sides are built.
- The event log survives a restart, so a count kept in it does too. `cost.recorded` is the usual
  spend row, but it is L3's alone (the `cost_recorded_l3_only` canary scans `jarvis/execution/`),
  and a tool is L4.

## Decision

Add `map_search`, count every Google map request in the event log, stop paid calls near a
monthly cap, and put the places a turn's tools returned on its answer.

- **`map_search`** (read-only, brain): `query` is a Text Search biased to a circle round his
  latest position (default 2 km); `kind` alone is a Nearby Search ranked by distance; `area`
  searches "<query> in <area>" with no bias; `place_id` alone is Place Details with weekly
  hours and up to three reviews. Each place carries name, address, distance and compass
  direction from him (straight line), open now, today's line of hours, rating, price level,
  phone, a maps link and `place_id`; what Google did not give is left out. A phone report older
  than 30 minutes adds a warning. The rule for a vague or misheard name (search first, name the
  specific match, let him confirm, never invent a place) is in the tool description only.
  A 403 `SERVICE_DISABLED` is the tool error "Places API (New) is not enabled", code
  `places_not_enabled`; the key travels in a header and appears in no result or error.
- **`bus`** keeps its name. Its result gains `destination` (`name`, `address`, `place_id`, `lat`,
  `lng`) as Google resolved it, and the summary says the name and address. The id comes from
  `geocodingResults`; the name from the `map_search` result held in memory for that id, else one
  Place Details (name, address, point; Pro). A kept place (home, school) is said as he named
  it and costs no lookup. A new `place_id` argument takes a `map_search` id as the destination.
  The summary says the walk the whole way, from the existing walking request.
- **Spend**: each request is priced from constants in `jarvis/execution/maps_spend.py` and
  recorded as a `maps.spend_recorded` row (`sku`, `requests`, `cost_usd`).
  Before a paid call the month's sum (Pacific month) is read from the log; from 90% of
  `maps.monthly_usd` (default 10; 0 is no cap) the call is refused with a plain reason she can
  say, and no request is made. Routes requests of `bus` count; a refused HTTP 4xx does not.
- **Places on answers**: a tool that returns places logs `map.places_found`
  (`places: [{name, lat, lng, address, place_id, kind: "place"}]`) on the turn. The answer's
  `surface.response_emitted` payload, and so its `/phone/ws` `row`, carries `places`: the turn's
  places, at most 8, the latest kept, none twice. The key is absent when there are none.
  `docs/spec.html` states the contract.

Field masks (the cheapest tier that covers the fields): search is Enterprise, details Enterprise
+ Atmosphere, name lookup Pro. The exact lists are `SEARCH_MASK`, `DETAILS_MASK` and `NAME_MASK`
in `jarvis/execution/places_api.py`.

## Alternatives rejected

- **Geocoding API for the destination's address** — the same project had it denied
  (`REQUEST_DENIED`, ADR 0237), and it would resolve the text a second time instead of reporting
  what Routes used.
- **Pull places from the tools' result text when the answer is written** — a result is windowed
  at 8192 characters and marked `truncated` when long (ADR 0019); a `map_search` of 8 places
  with a place_id lookup can cross it. A logged event is exact.
- **Record it as `cost.recorded`** — the daily spend card would have counted it for free, but the
  `cost_recorded_l3_only` canary fails any `emit_event` of that type under `jarvis/execution/`,
  and the tools that spend are there.
- **A new table beside the event log** — a second store for a sum the log already holds, with
  its own migration, for no reader the log's index cannot serve.
- **A cap on the model's number of calls per turn** — one `bus` search costs 0.02 USD and one
  `map_search` 0.035 USD, a ratio no call count expresses; the cap he can reason about is dollars.
- **Put the vague-name rule in a prompt** — it is a rule about this tool's results and travels
  with the tool, so a turn that never loads the tool carries nothing for it.

## Consequences

- The unit prices are Google's list prices as remembered from the March 2025 change, without
  the free monthly allowance, so the count runs high; transit Routes is counted at Pro, 10 USD per
  thousand, because it was not certain it is Essentials. The cap may stop calls early.
- A search can return a different branch than he meant; confirming the match is up to the model
  and the tool description, not code.
- Whether Places API (New) is enabled for his key is not known until the first real call.
- Map dollars are not in the daily spend card (ADR 0185), which sums `cost.recorded`; they have
  the monthly cap alone.
- `bus` costs one more request (0.017 USD) for a free-text destination whose id was not found by
  `map_search` in this process; a restart forgets which ids were found.
- A turn that returned places only to dismiss them still sends them; the phone decides what to
  draw.
