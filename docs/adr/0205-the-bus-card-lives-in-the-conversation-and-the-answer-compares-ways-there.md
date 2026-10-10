# ADR 0205 — The bus card lives in the conversation, and the answer compares ways there

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- 2026-10-09, Allen: when he chats with her in the Dashboard, every card she brings up belongs in
  that chat window, not up on the notch; only the thing he finally pins goes on the notch. ADR 0203
  drew the `transit` options as a card under the notch, served from memory for 60 seconds.
- The conversation already has a card slot: the ask card (ADR 0066, 0144), an event in the log
  that the companion polls and draws in the talk area or on the Conversation page, that a newer
  card replaces, and whose closing rule is the one place that decides when a conversation card
  goes away.
- `transit` asked Google for the bus only. "How do I get there" is also "is it worth walking or
  driving", and the answer to that was one more question away.
- A pin of three trips (ADR 0204) refreshed each trip with its own download of BC Transit's
  realtime feed, three per 30 seconds for one feed.

## Decision

- **The bus rows are an ask card.** Every `transit` answer writes a `clarification.requested`
  with no fields and a `trip` (offer id, destination, the rows, the other ways). The card is the
  newest in the slot, so it is shown, replaced and closed by the ask card's rules, wherever the
  ask card is drawn (the talk area, the Conversation page, or the notch when the Dashboard is
  closed and no talk area is up). The companion draws a card with a `trip` as rows, not as a
  question; it asks the model nothing, so no "your card closed" note is written for it. The card's
  ✕ is the ask card's dismissal. The 60 second expiry and the `transit_offer` in
  `GET /inherent/notices` are gone; the read drops a row whose leave time has passed and the card
  with the last of them.
- **A row toggles a trip of the pin.** Its button adds that bus to the pin (at most three) and
  then reads "已挂"; pressing it again removes it. A newer answer's first press starts a new set
  (ADR 0204). The notch keeps only the pinned countdown, its detail card, next bus, cancel and
  undo (ADR 0203).
- **The answer compares ways there.** `transit` sends three requests at the same time, TRANSIT as
  before and DRIVE and WALK asking only for `routes.duration` and `routes.distanceMeters`, each
  with the 5 second timeout. The result gains `modes: {drive, walk}`, each `{minutes, km}`; a mode
  that fails is left out, and the bus answer alone decides whether the tool errors. The card
  shows them as one line above the rows, and the description tells her to mention an alternative
  only when it matters (walking under about 20 minutes, driving much faster).
- **One feed download per refresh.** `BusLive.snapshot()` downloads and parses the realtime feed
  once; the refresh matches every trip of the pin against it.

## Alternatives rejected

- **Keep the offer under the notch and add a second card in the chat** — the same rows in two
  places, with two lifetimes to keep in step.
- **A bus-card store next to the ask card's** — a second slot, a second poll and a second closing
  rule to change whenever the conversation's rule changes.
- **Bicycle as a fourth mode** — another billed request on every transit question for a mode he
  has not asked about.
- **Traffic-aware driving time** — a pricier request for a few minutes on a comparison line.

## Consequences

- One `clarification.requested` is written per `transit` answer; the ask-card fold reads the log
  incrementally, so the cost is one row.
- The rows' pin buttons need the daemon's in-memory offer (ADR 0203); after a restart the card may
  still be up, and a press on it answers 404 and the card says the bus is no longer on offer.
- A `transit` answer now makes three Google Routes requests instead of one. DRIVE and WALK are not
  counted against the bus answer's time (they run alongside it) but are billed.
- A new ask from the model replaces the bus card, and the other way round.
