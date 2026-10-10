# ADR 0202 — The notch offers the bus trip as a card and pins it without the model

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- 2026-10-09, Allen: pinning (ADR 0200) took a spoken "yes" to her question, a model call that
  copied six fields out of the conversation, and a second click on the pill to take it off with no
  undo. He wants one click to pin, and a pill that opens to something he can act on.
- Everything a pin needs is already in the `transit` answer and in the closure that kept each
  option's first stop; nothing needs the model once the answer exists.
- The offer is not state worth keeping: it is only useful for a minute, and a restart losing it
  costs one more `transit` question. The pin it creates is event-log state (ADR 0200).

## Decision

After every `transit` answer the daemon keeps the options in memory and serves them as
`transit_offer` in `GET /inherent/notices` for 60 seconds, omitting any option whose leave time
has passed; the companion shows them as a card under the notch with a button per row, and a click
is `POST /inherent/departure` that pins that option through the same function as `pin_departure`,
with no model call. A newer answer replaces the offer, and a stale, expired or unknown offer is a
404 that only closes the card. Clicking the pin pill opens a card with the trip, the live refresh's
status, and two actions through the same route: next bus (the last answer's later option on the
same route and stop, else BC Transit's feed) and cancel, which shows an undo for 5 seconds that
pins the same trip again. `transit` no longer asks whether to pin; `pin_departure` stays for when
he asks by voice.

## Alternatives rejected

- **The model offers and pins on his "yes"** — the status quo: two model turns and a copy of six
  fields per pin, and b28c0ec9 already had to forbid her a search and a second lookup first.
- **Store the offer in the event log** — a card that lives 60 s would add one event per
  `transit` call to a log that is folded whole by several readers, for state nothing reads
  after a minute.
- **Keep click-then-✕ on the pill** — a second click that destroys the pin with no undo; the
  next-bus action needs a surface anyway, and a card is where the notch already puts actions.

## Consequences

- The offer lives in the daemon process, shared by the `transit` tool and the routes through one
  object the runtime builds; a second daemon would not see it.
- "Next bus" from the last answer can be a bus Google planned minutes ago; the live refresh then
  corrects it as for any pin. From the feed it needs the first stop's position, so a pin made
  before the daemon last started has no next bus.
- The undo remembers only the last unpinned trip, in memory.
