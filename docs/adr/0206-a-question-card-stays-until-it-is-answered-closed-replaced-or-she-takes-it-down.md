# ADR 0206 — A question card stays until it is answered, closed, replaced or she takes it down

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** 0066

## Context

- ADR 0066 gave the decision model `ask_user`, a card with a question and
  one to four fields, and `remember`. It closed a waiting card on Allen's
  next utterance, so that a spoken answer would not leave a stale card.
- On 2026-10-09 Allen said 「帮我连上 Linear」, heard as "Linia". She put up
  「你说的 Linia 是指 Linear 吗？」 about seven seconds later. He spoke again
  4.8 s after it appeared, and the card vanished under his words before she
  had handled them. He asked that a card raised in a conversation not
  disappear on its own: it goes when he closes it, or when she knows it was
  wrong or answered.
- About three quarters of his input is voice (ADR 0062), so he often talks
  while a card is up, and not always to answer it.
- The confirmation card already waits for its button and has
  `withdraw_card` for her to take it down (ADR 0062).
- The plugin panel's request is replaced by the next `open_plugin` while it
  is only offered, so a second ask for the same app resets the panel he is
  looking at.

## Decision

`ask_user` and `remember` stay as ADR 0066 made them, except how the card
closes. A question card closes only when:

- he submits it, which runs a turn with his answers;
- he closes it with ×; the next turn is told it was closed unanswered;
- she asks a newer question, which replaces it; or
- she takes it down with a tool, because his words answered it or it no
  longer applies.

While it waits, every turn he starts by speaking or typing is told the card
is still up and what it asks, so she can carry on with a spoken answer and
take the card down in the same step.

A plugin request he has been offered is kept, not replaced, when she opens
the same app again; a different app still replaces it.

The limits of ADR 0066 hold: no field asks for a password, card number,
code or key; GPT-Live does not get these tools; the card only asks and
never approves an action.

## Alternatives rejected

- **Close on his next utterance (ADR 0066)** — the 2026-10-09 run shows the
  card gone before she had read his words; when they were not an answer,
  the question was lost and had to be asked again.
- **Close automatically once she has replied to his words** — closes a card
  his words did not answer, which is the same loss one turn later.
- **Reuse `withdraw_card`** — it rejects a confirmation and records an
  answer to an action; a question card has no action, and one tool with two
  meanings is easier for the model to misuse.

## Consequences

- A card she forgets to take down stays until he closes it or she asks
  again.
- Each turn while a card waits carries one more note line in the packet.
- The bus card of ADR 0205 follows these rules too: it stays while he talks.
- ADR 0144's hold still hides a fragment's card for its window; the rest of
  his sentence no longer closes it.
- Taking a card down is one more tool call when his words answered it.
- Every remembered fact still goes to the model provider in the system
  prompt of every request, as under ADR 0066.
