# ADR 0072 — A filled-in card counts as one of Allen's sentences

**Status:** Superseded-by-0074
**Date:** 2026-09-26
**Supersedes:** 0053

## Context

- ADR 0053: in conversation mode a sentence with a pause in it arrives as two
  utterances, each its own turn. The first half's answer is ready while
  Allen is still saying the second half. Allen's rule (2026-09-25): she does
  not start talking while he is talking. When he stops with a real sentence,
  her unspoken answer to his previous sentence is dropped, and both are
  answered as one. A dropped answer is not heard, not in the record, and not
  in the next turn's history. Once she is audible, barge-in owns the turn.
- ADR-0008 D1: a run is terminal at generation completion, and memory.db
  gets Jarvis's row right after render. ADR 0044 folds an unanswered `allen`
  row into the next user message.
- ADR 0066: a filled-in ask card is recorded as Allen's words and starts a
  turn of its own (`surface.user_intent`, channel `clarify`).
- 2026-09-26 22:13, live: Allen filled one of three fields and pressed Done,
  then said 「内容你自己定吧」 while the card's turn was running. Only voice
  turns could be dropped, so the card turn's answer, 「请告诉我邮件主题和想表达的
  大意」, played for 4 s after he had answered it. Then the next turn's
  confirmation cut it off.

## Decision

While any of Allen's utterances is in flight (from speech onset until it is
accepted or comes to nothing), no response run completes and no answer starts
playing. When one is accepted, before its `utterance.received` is written,
each open run of the last 10 s is cancelled with reason `superseded` and its
queued audio discarded if all of these hold:

- it answers another of his voice sentences or a filled-in ask card;
- none of its answer has reached the speaker.

Limits:

- Only runs whose interrupt policy lets generation be cancelled.
- A turn any of whose audio has played is never dropped (barge-in owns it).
- Typed text and card buttons are never dropped.
- A hold that is never released delays completion by at most 60 s.

## Alternatives rejected

- **Voice sentences only (ADR 0053 as written)** — the 22:13 run above: a
  stale question played over the answer Allen had just given.
- **A longer pause before a turn ends** (`required_misses` 24 → 32, 0.77 →
  1.02 s) — every answer starts 0.25 s later, and a hesitation longer than the
  new pause still splits the sentence the same way.
- **Keep the dropped answer in memory.db, marked unspoken** — the next turn's
  history then shows Jarvis asking a question, and the model reads Allen's
  next sentence as a reply to it (Allen's pick, 2026-09-25).
- **Drop in the intent pump when the new turn arrives** — the pump can start
  the new turn before the drop lands, so the new prompt could still carry the
  dropped answer.

## Consequences

- Every answer, a background report included, waits while Allen talks, for up
  to the utterance's 30 s cap.
- Within 10 s, a new sentence cancels a tool turn Allen just asked for,
  whether he asked by voice or on a card. Actions it already dispatched are not
  undone. The new turn's prompt carries both, so the model may dispatch them
  again.
- The card's answer turn is where its status line says which fields were
  left blank. When that turn is dropped, the next turn gets Allen's answers
  and his new sentence without that line.
- A streamed answer's text reaches the surfaces before its run completes, so
  a dropped one's words may already be on the wire. Surfaces must not show
  answer text while Allen's words are in flight.
- A run that passed the completion hold just before Allen started talking can
  complete between the drop and its cancel. Its queued audio is then lost
  while its row stays. The window is milliseconds wide.
