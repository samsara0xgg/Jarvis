# ADR 0062 — A confirmation is a card that waits for its button

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** 0039

## Context

- On 2026-09-26 Allen tested the Gmail send by voice. The model asked in its
  own words, the runtime then asked again with a line holding the raw
  arguments as JSON, and the result came back as a JSON excerpt. The prompt
  line "show the plan or content first, then ask for confirmation" caused
  the first ask. ADR-0012 already makes the runtime's ask the only one that
  counts.
- The same day, on the design page (artifact 27F1JtWJmVk6TJMdC689AU, v4),
  Allen decided the following:
  - An action with consequences shows as a card in the Conversation page.
    With the page closed, the card grows from the notch.
  - A letter on a card is edited in place, and pressing send sends what the
    card holds at that moment.
  - A card stays until it is sent or dismissed with its ×. It never expires.
  - "改一下" revises the same card.
  - A reversible small action runs without a card.
  - On 2026-09-26 he approved the three proposals that went with this: a
    pending card sits above the input, a spoken 「发」 counts only right
    after the card appears or changes, and a card survives a restart.
- ADR 0039 closed an ask on the first utterance that did not answer it,
  because 「好」 said to a later question fired an earlier ask (audit H1-01).
  That hazard exists only for words. A button names the exact confirmation
  it answers.
- The confirmation slot is folded from `events.db` on every read, so it
  already survives a restart. The authorized-dispatch outbox checks that the
  dispatched arguments are exactly the accepted snapshot's `args_meta`, in
  four places.
- The companion reaches the daemon over the v1 HTTP routes with the local
  key. ADR-0014's v2 socket, which has confirmation deltas, is off
  (`inherent.v2_sequencer.enabled: false`), and no client uses it.

## Decision

A pending confirmation is a card that waits for Allen's button. The card
never expires. It closes when it is accepted or dismissed, or when a newer
ask replaces it; only one card waits at a time.

- **Words.** The yes/no grammar answers the card only on the first user
  utterance after the ask, and only within `confirmation.ttl_ms`. Any other
  utterance leaves the card waiting and runs as an ordinary turn. The model
  sees the card and its arguments in the pending note, and can revise or
  re-ask only by proposing the tool again, which replaces the card.
- **Buttons.** A button is a `surface.user_intent` carrying
  `confirmation_decision` with the card's id. It runs no grammar and
  writes no row of Allen's words.
- **Edits.** Accepting with edits freezes the edited arguments as a new
  `confirmation.requested` for the same tool and target, then accepts that
  one. Only an existing string argument can be replaced, and only with a
  string.
- **What is said.** The ask is one runtime line (`confirm.ask_letter` for a
  letter, otherwise `confirm.ask_tool` with the tool's name from the
  language table). The result is that tool's ack line. Neither shows
  arguments or JSON.

## Alternatives rejected

- **Keep ADR 0039 and make the button answer only the next turn** — about
  three quarters of Allen's input since late August was voice (the design
  page's count from `mac_events.db`). The first unrelated sentence would
  kill a letter he is still editing, which is the loss he named
  ("写了一半的草稿不该跟着丢").
- **Let the client submit 「可以」 through `/inherent/submit`** — the text is
  unscoped. It answers whatever slot is current when it lands, and ADR 0039
  refuses it after any other utterance. A button carries the id it was
  drawn for, and the route returns 409 when that card is gone.
- **Apply edits at dispatch time on top of the frozen snapshot** — the outbox
  compares dispatched arguments with the accepted snapshot in four places.
  Each would need the overlay, and the event log would no longer show one
  snapshot equal to what ran.
- **Build it on the v2 socket's `confirmation.upsert` (ADR-0014 D14)** — v2
  is off in production and no client speaks it. D14 also withholds the
  arguments, which the card has to show in order to be edited.

## Consequences

- Words cannot send a card that has been waiting past its first utterance.
  Allen has to press the button or ask again, and asking again puts up a
  fresh card.
- While a card waits, every turn takes the full tool path (`pre_route`
  returns `unknown`), and new commentary stays silent.
- A newer ask replaces a waiting card without saying so. There is no queue.
- The model no longer sees what a confirmed tool returned. It sees only the
  ack line, so a created page's link or an issue number has to be looked up
  again.
- Loosening a plugin's approval mode (Hue lights) stays a per-plugin
  setting. This decision does not change which tools ask.
