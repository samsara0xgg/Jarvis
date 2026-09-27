# ADR 0075 — Jarvis takes a card down when Allen says not to go ahead

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- ADR 0062: an action that needs approval waits on a card until it is
  accepted, dismissed with its ×, or replaced by a newer ask. The yes/no
  grammar answers the card only on the first utterance after the ask, and
  only as an exact sentence. The model sees the waiting card in a note and
  can change it only by calling its tool again.
- ADR-0012 D4: the note carries no card id, so the model has no handle to
  authorize the card. Only Allen's button or his exact yes can.
- 2026-09-26 23:33, live: a letter card waited, and Allen said
  「OK撤销吧，不发了。」 The grammar did not match the sentence. The model had no
  way to close the card and answered 「好的，撤销这封邮件，不发送。」 The card
  stayed until he pressed its button 10 s later.
- 2026-09-26 22:38, live: after 「不是给我自己」 the model had no friend's
  address to put on a new card. It answered that the letter "is still
  waiting for your confirmation" and left the wrong card up.
- Allen, same night: when a card is up, he can talk to Jarvis to change the
  card's state and content, and he asked for a way to take the card down.
- `answer_confirmation_once` is the one writer of a card's answer: one answer
  per ask, re-read under the write lock.

## Decision

The model gets a `withdraw_card` tool that rejects the waiting card with rule
`card_withdrawn`, so its action never runs.

Limits:

- It takes no arguments and acts only on the card waiting now. With no card
  waiting it fails, so the model cannot claim to have withdrawn one.
- It can only close a card. Accepting stays with the button and the grammar.
- Changing a card is still done by calling its tool again.

## Alternatives rejected

- **Widen the no-grammar to catch sentences like 「撤销吧，不发了」** — the
  grammar matches whole sentences so that a 「好」 meant for another question
  never fires a card (ADR 0039, 0062). Each new phrasing needs a new entry,
  and 22:38's 「不是给我自己」 names no cancel word at all.
- **Close the card on the first unrelated utterance** — ADR 0062 rejected this:
  a letter Allen is still editing on the card would be lost to his next
  sentence.
- **Give the note the card's id and let the model answer it through the
  answer path** — the same handle would let a model output reach acceptance,
  which ADR-0012 D4 keeps out of its reach.

## Consequences

- The model decides when Allen's words mean "take it down". A misread
  sentence can close a letter he was still editing; he has to ask again.
- The rejection row is attributed to Allen (`actor` user, no words), because
  his words are what the model acted on.
- When the model can call the tool again with a blank argument, it may put up
  a new card instead of taking the old one down. On 2026-09-26, 「不对，不是发给
  他，是发给我另一个朋友」 got a new card with an empty `to` in 3 of 3 live runs.
  The card cannot edit `to`, so that card cannot be sent as it stands.
- Every request carries one more tool definition.
