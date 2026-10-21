# ADR 0350 — She asks in words on a spoken turn, a question ends its turn, and an answer is kept only when marked

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0206, keeping ADR 0066's card for a question asked on any turn, and its keeping of every filled field the model does not mark as one-off

## Context

- `ask_user` puts up a card with a question and one to four fields (ADR 0066). The card stays until
  he submits it, closes it, she asks a newer question, or she takes it down (ADR 0206). It shows
  only on the device whose turn raised it (ADR 0218).
- Two lines send every clarification to the card:
  - the tool's description: use it "whenever your reply would otherwise end by asking for such
    details, even where you asked in words before";
  - the system prompt: "ask through ask_user rather than in your reply".
- What that did in his use:
  - 2026-10-16: a misheard sentence (「我让你叫我」) became a card.
  - 2026-10-18: asked when a store closes, with the store's name misheard, she put up a card
    asking for the name and his rough location. She knew where he was.
  - 2026-10-18: she asked the same question twice. "The turn then ends" is a sentence in the
    description. Code ends a turn only after a confirmation card. The result's `next` asks the
    model for one more request, in which she can say or ask again.
- About three quarters of his input is voice (ADR 0062). A card from a spoken turn lands on the
  screen of the device he spoke to, which he may not be facing. On 2026-10-09 he asked that a card
  not vanish under his words (ADR 0206), so such a card waits until she takes it down.
- A filled field goes into `[About the user]` unless the model marks it one-off: `remember`
  defaults to true in the tool and where the card's answers are stored. Every remembered fact goes
  to the model provider in every request.
- The text before a call is never spoken or shown (ADR 0320).
- The tool menu is part of the cached prompt prefix (ADR 0225). It is the same for spoken and typed
  turns.
- On 2026-10-21 Allen approved the plan for this card (「就按你说的去吧」):
  - on a spoken turn, ask once, in words;
  - don't ask about misheard words or what the state already holds;
  - asking ends the turn, guaranteed by code;
  - nothing is kept unless marked.

## Decision

Put a question on a card only for a turn he typed, and end the turn in code once she has asked.

- **A typed turn** gets the card, on the device he typed on. Her line is the card's question.
- **A spoken turn** gets no card. Her line is the question, said as written. When the question has
  a single field with choices and does not already name them, the choices follow it, joined as the
  zh/en table words a list of options.
- **The ask ends the turn.** No model request follows the call. His answer comes as his next words
  or as the card's submission.
- **Kept only when marked.** A field goes into `[About the user]` only when she marks it
  `remember: true`. A missing mark means not kept.
- **No steer.** The tool's description says what the card is, when it does not apply and what it
  returns. The system prompt does not send her questions to the tool. Neither says when to ask.
- **Unchanged.** A card closes as ADR 0206 says, shows where ADR 0218 says, never asks for a secret,
  and never approves an action.

## Alternatives rejected

- **Take `ask_user` off the menu on a spoken turn.** The menu heads the cached prefix. A menu that
  changes with the channel misses the cache on every switch between typing and talking, and a
  quarter of his input is typed (ADR 0062).
- **Refuse the call on a spoken turn and let her ask again in words.** That is one more model request
  to say what the call already holds. In his use, an extra request costs about 2.4 s before the
  first word.
- **Close the card on his next words (ADR 0066).** On 2026-10-09 the card vanished before she had read
  his words, and he asked for the opposite.
- **Refuse a field whose label names something the state holds.** Labels are free text in two
  languages. A list that refuses 「位置」 misses 「大概在哪」, and it also refuses 「送到哪个地址」, a fair
  question when the address is not his home.

## Consequences

- A spoken question with several details is one sentence. He answers what he heard, and she asks
  again for what is still missing.
- What she wrote before the call is lost. A typed ask shows only the question and the card.
- An address she forgets to mark is asked again next time.
- Whether she asks at all, about a misheard word or about something she could look up, is still her
  choice. Only the whole-turn exam watches it.
