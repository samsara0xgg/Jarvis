# ADR 0131 — What Allen starts shows in the talk area, what she starts stays in the notch

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Allen, 2026-10-02: during a conversation a
  confirmation card (ADR 0062) or a question card (ADR 0066) sent her back
  into the island, the talk area under her folded away, and the card grew
  from the notch, so his eyes had to move from the area to the notch in the
  middle of one exchange. He agreed to the rule "whoever speaks first decides
  where it shows" and asked that her timed outfit change stop bringing her
  out of the island, which he found distracting while working.
- The talk area (ADR 0113) is up only while a conversation he started is on,
  and is gone 8 s after it ends. The notch already holds everything that
  arrives without him asking: the agents' notices (ADR 0057, 0069), the
  night run's cards (ADR 0093), and cards that come when no conversation is
  up. Notices are held while she talks and come up after.
- The `.ac` card already has two hosts (the Conversation page's glass and
  the notch's black); it needs no new component to sit on a third surface.

## Decision

While the talk area is up for a conversation Allen started, everything that
conversation produces, its confirmation and question cards included, shows in
the area and she stays out under the island; anything that arrives without
him asking shows only in the notch, and she does not come out for it.

Its limits:

- A card that arrives while the area is not up still grows from the notch
  as ADR 0062 describes; the area is never opened for it.
- A waiting card keeps the area up; it folds only after the card is answered
  or dismissed and the usual 8 s pass.
- Her own timed outfit change happens where she is, in the island. A skin
  Allen picks still brings her out to show it.

## Alternatives rejected

- **Everything in the notch, the island growing into a 360 px panel for
  conversations too.** One place on screen would move instead of two, but
  she would no longer come out to talk; Allen chose the rule above over it
  (2026-10-02).
- **Keep the card in the notch and leave the area open under her.** The
  exchange would still be split across two surfaces about 200 px apart, the
  eye movement Allen reported; the area alone has room for a letter card.

## Consequences

- The area must grow to fit a letter card within its 360 px cap and scroll
  past it, and the card's ⌘⏎ and editable fields must work outside the
  notch pane.
