# ADR 0237 — A place kept as an address is located once from it through Google's geocoder

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** the address clause of ADR 0234: that a place kept as an address alone gets its centre only when he names it on the spot.

## Context

- ADR 0234 moved home and school from the `transit.home` and `transit.school` settings into the
  places table. A setting written as `lat,lng` became a centre; an address became an address
  alone. A stay belongs to a place only by its distance from the place's centre.
- After the deploy at 13:25 PT on 2026-10-11, the Pi's table held home and school as addresses,
  with `lat` and `lng` NULL. No stay can match either, so the day ring cannot colour his two
  most visited places and the ledger keeps the phone's street names for them.
- ADR 0234's only way out is a conversation at the place: she asks about the unkept spot, he says
  what it is, and `save_place` fills the centre. Until then, nothing matches.
- The same addresses already go to Google, with the key in `GOOGLE_MAPS_API_KEY`, in every
  `transit` request (ADR 0189).
- A geocoder can answer with a region (precision `APPROXIMATE`) or with several places. Either
  as a centre would claim stays that are not his place.
- The key's Google Cloud project has only the Routes API enabled. A probe from the Pi at 20:30Z
  got `REQUEST_DENIED`, "This API is not activated on your API project".
- The Geocoding API documents its key as the `key` URL query parameter.
- At 20:47Z Allen chose looking the addresses up, on the decision card in thread
  「ios手机端的功能建设」 on 2026-10-11.

## Decision

Look each place kept as an address up once with Google's Geocoding API, and keep the single point
it gives as the place's centre, as a versioned change of origin `geocoder`.

- Boot starts it in the background, with the transit key, for each kept place that has an
  address, no centre and no geocoder change.
- Only a single result whose precision is not `APPROXIMATE` is kept. A refused answer or a failed
  request writes nothing, and the next boot asks again.
- A centre he gave first is left as it is. Undoing the geocoder's change makes the place an
  address again, and it is not looked up again.
- The radius stays the place's own.

## Alternatives rejected

- **Wait for him to name it there (ADR 0234 as written)**: both places stayed unmatched after the
  deploy, which is reproducible on the Pi by reading the places table before any such
  conversation. They are the two places he spends most days at.
- **The Routes API's leg start location (already enabled)**: Google documents that point as
  snapped to the nearest road when the origin is not on one. A campus centre can be farther
  than 120 m from its nearest road, so the circle would miss the place it names.
- **Look it up on the phone (Apple's geocoder, no key)**: the table lives on the brain. The phone
  would read address-only places, look them up and write back, a second writer across devices
  for two rows, beside the brain's own writers.
- **OpenStreetMap's Nominatim (no key)**: it sends his home address to a third party that gets
  nothing from him today. Google already receives it.

## Consequences

- The Geocoding API has to be enabled on the key's project. Until it is, each boot sends one
  refused request per place kept as an address and logs a warning.
- A point Google puts at one edge of a campus leaves stays at the other edge unmatched. Undoing
  the change returns the place to an address, which then waits for him to name it there.
- An address Google cannot place precisely costs one request per boot until he names it there.
- The key travels in the request URL. The code keeps that URL out of every exception and log.
