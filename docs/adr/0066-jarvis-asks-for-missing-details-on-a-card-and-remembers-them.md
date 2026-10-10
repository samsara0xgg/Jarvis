# ADR 0066 — Jarvis asks for missing details on a card and remembers them

**Status:** Superseded-by-0206
**Date:** 2026-09-26
**Supersedes:** none

## Context

- On 2026-09-26 Allen asked Jarvis to find food on Uber Eats. Jarvis asked
  for the delivery address in a sentence. Allen said the address aloud and
  added 「把这个记下」. The model answered that it would not store a full
  address and called no tool.
  - Nothing in `prompts/jarvis_v1.md` forbids storing it, and no gate fired.
  - The model has no writer for the `[About the user]` block. Its rows come
    from first-run name setup and from hand edits.
  - `create_memo` lands in the memo inbox, which never reaches the prompt.
    `save_knowledge` needs a source record id, and the model is never shown
    the current utterance's id.
- Allen decided the following the same day:
  - Missing details come as a card with input boxes, not as a question in
    the chat.
  - The card is a tool the model calls.
  - What he fills in is remembered: 「本来这就是每个人本地的」. memory.db
    stays on each Mac.
- A turn cannot wait for a person. The intent worker drives one turn at a
  time. The confirmation card already ends its turn, and its button starts
  a new one (ADR 0062). The plugin 「连接并继续」 button resumes the original
  request as a fresh `surface.user_intent`.
- About three quarters of Allen's input is voice (ADR 0062's count), so he
  may answer out loud while a card is up.
- Two outside designs have the same shape: a tool call yields a small form,
  and the answers come back.
  - Codex's `request_user_input` is off by default outside plan mode,
    because models over-ask.
  - MCP elicitation's form mode forbids asking for passwords or keys.
- The `[About the user]` block ends the cached system prompt. A new row
  costs one prefix-cache miss and is seen from the next turn.
- ADR 0062 left "fill" cards out on purpose. The spec already names
  `surface.clarified` and `surface.dismissed` as the durable forms of such
  an answer, with no emitter.

## Decision

The decision model gets two tools.

- `ask_user` puts up one card with a question and one to four fields. A
  field is free text or a pick-one choice. The turn then ends.
  - Submitting the card records the answers as Allen's words and runs a
    turn with them.
  - The × closes the card, and nothing runs. The next turn is told it was
    closed unanswered.
  - A newer ask replaces the card.
  - Allen's next utterance also closes the card, and that turn is told
    what was asked, so a spoken answer works.
  - Every filled field is remembered in `[About the user]` under its label,
    unless the model marked the field as one-off.
- `remember` keeps one fact under a topic, and a later fact with the same
  topic replaces it.

Limits:

- A field may not ask for a password, card number, code or key. The tool
  refuses such a field.
- GPT-Live does not get these tools.
- The card only asks. It never approves an action; confirmations stay with
  ADR 0062.

## Alternatives rejected

- **A skill that tells the model to ask for an address** — a skill is text
  the model reads (ADR 0035). It cannot draw an input box, so the question
  would still arrive as a chat sentence.
- **Keep the turn open until the card is answered** — the intent worker
  runs one turn at a time. A turn waiting on a person would leave every
  later utterance unanswered until the card is filled. That is the reason
  the confirmation card ends its turn.
- **A 「记住」 tick on each field, off by default** — Allen chose on
  2026-09-26 to remember by default. The data never leaves his Mac except
  inside requests to the model provider, which already carry the whole
  conversation.
- **Remember through `save_knowledge`** — it requires a `source_refs`
  record id that the model is never shown. Knowledge is also searched on
  demand, not placed in the prompt, so the next turn would ask again.
- **Keep the card until it is answered or dismissed, as the confirmation
  card does** — a spoken answer would leave a stale card on screen. The
  confirmation card waits because losing a half-edited letter costs work.
  A closed question costs one re-ask.

## Consequences

- Every remembered fact, an address included, goes to the model provider
  in the system prompt of every request.
- A wrong fact stays until the same topic is written again or the row is
  deleted by hand. There is no forget tool.
- `profile` rows are no longer append-only. A fact row is rewritten in
  place.
- The model still decides when to ask. It can still ask in words, and no
  prompt line is added to steer it.
- An unrelated utterance closes a waiting card, and the model has to ask
  again.
- The secret refusal matches words in the question and labels. A field
  phrased around the list gets through.
