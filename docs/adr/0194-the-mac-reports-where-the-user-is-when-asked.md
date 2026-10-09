# ADR 0194 — The Mac reports where the user is, when asked

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- Allen carries the MacBook the daemon runs on, so its position is his. Questions like "when's
  my bus home" or "what's near me" depend on it, and the `transit` tool (ADR 0189) only knew
  the saved `home` and `school`.
- A bare command-line binary never gets a macOS location prompt; an `LSUIElement` app bundle
  launched with `open` does. Checked live on this Mac: the bundle returned a fix of about 40 m
  in under 2 s once authorized.
- A rebuilt app has a new code signature, so macOS may ask for permission again.
- A phone can report where he is when the Mac is at home. That is a separate source, not part
  of this decision.

## Decision

- Add `native/where`, a Swift app bundle built on demand and ad-hoc signed with the fixed
  identifier `com.allen.jarvis.where`. Each read launches it once: one CoreLocation fix, an
  Apple reverse-geocoded place label when that arrives within 4 s, then it exits (10 s ceiling).
  Nothing is cached and nothing tracks in the background.
- `jarvis.surface.mac_location` runs it and returns latitude, longitude, accuracy and an
  optional place, or raises with the reason (denied, timed out, failed). The runtime hands it
  to the tools only on macOS and not in the `brain` role.
- `transit` accepts `here` as origin or destination, and the model is told to use it when he
  does not say where he starts. `from`/`to` say it came from this Mac, with its accuracy.
- A read-only `where_am_i` tool (L1, no confirmation, no wait line, no Google key needed) returns
  the same reading. The model calls it only when the question depends on where he is now.
- When the location cannot be read, both tools return an error telling the model to ask where
  he is or to use a saved place he names.

## Alternatives rejected

- **A command-line helper** — it never triggered the permission prompt in the live check on this
  Mac, so it could only ever return "denied".
- **Cache the last fix or track continuously** — he did not ask for tracking; a fresh read costs
  under 2 s, and an old fix would put a bus trip at the wrong stop.
- **Google Geolocation or a Google reverse-geocode for the place name** — a paid, keyed call;
  Apple's geocoder on the Mac is free and needs no key.

## Consequences

- Location leaves the Mac in two ways: as latitude and longitude in the Google Routes request
  `transit` already makes, and inside the tool result that goes to the model with the next
  request. The reverse-geocode sends the fix to Apple.
- The first read after any rebuild of the helper may show a permission dialog on screen.
- Off macOS, in the `brain` role, or with permission denied, `here` is a tool error and
  `where_am_i` is absent or errors; a terminal-side or phone source would have to be added.
- A tool has no way to append its own event, so a read is logged only as the `action.result_observed`
  the dispatcher already writes for `where_am_i` (latitude, longitude, place, accuracy), which makes
  the event log hold his position for those calls. A location history ("where did I go
  yesterday") would hang off the event log later, fed by a phone stream; nothing here builds it.
