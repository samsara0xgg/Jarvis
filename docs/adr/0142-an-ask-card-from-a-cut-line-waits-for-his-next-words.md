# ADR 0142 — An ask card from a cut line waits for his next words

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- ADR 0074: a line cut short while she speaks (endpoint reason `barge_pause`,
  he paused for `barge_in_pause_ms`) is its own turn, and the rest of his
  sentence arriving a moment later is folded with it. An ask card (ADR 0066)
  stays up until a newer utterance talks over it.
- Live 2026-10-03 15:04 PDT, turn T4cb129a5: 「帮我总结下。」 was cut as a
  `barge_pause` fragment at 22:04:09.0Z; the model put up an ask card at
  22:04:11.9Z; the rest of his sentence arrived at 22:04:13.3Z (1.4 s later)
  and closed it. The card was on screen for 1.4 s for nothing.
- The companion shows the card by polling `/inherent/clarification`, which
  folds the event log. Every ask card reaches the screen through that read.

## Decision

The ask-card read hides a card for 1.5 s (`_FRAGMENT_CARD_HOLD_MS`) after its
`clarification.requested` when the turn that asked began from an utterance
whose endpoint reason is `barge_pause`.

Its limits:

- Only the display is held. The card, its event and the model's turn are
  unchanged, and a card from any other turn shows at once.
- A continuation inside the hold closes the card as it did before, so it is
  never shown. After the hold a card still waiting shows.

## Alternatives rejected

- **Delay writing `clarification.requested`** — the model's next turn and
  the pending-card note (ADR 0066) read that event; the turn above had
  already ended 0.5 s after it, so a delayed write breaks them.
- **Hold every ask card** — a card from a complete line would show late;
  only a fragment is likely to be followed by its other half.

## Consequences

- A card from a fragment appears up to 1.5 s late, plus one poll interval,
  even when no continuation follows.
- The hold ends by the clock, not by speech: his continuation still
  speaking but not yet transcribed at 1.5 s does not keep it hidden.
