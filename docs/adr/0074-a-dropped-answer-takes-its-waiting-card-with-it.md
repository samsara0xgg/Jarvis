# ADR 0074 — A dropped answer takes its waiting card with it

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** 0072

## Context

- ADR 0072: in conversation mode a sentence with a pause in it arrives as two
  utterances, each its own turn. Allen's rule (2026-09-25): she does not
  start talking while he is talking. When he stops with a real sentence, her
  unspoken answer to his previous sentence is dropped, and both are answered
  as one. A dropped answer is not heard, not in the record, and not in the
  next turn's history. Once she is audible, barge-in owns the turn. A
  filled-in ask card (ADR 0066) counts as one of his sentences.
- ADR 0062: a confirmation is a card that closes only when it is accepted,
  dismissed with its ×, or replaced by a newer ask. Only one card waits. The
  model sees the waiting card and its arguments in a note on every turn.
- A turn that proposes a tool needing approval writes
  `confirmation.requested` mid-turn, before its run completes. The drop
  cancels the run; it did not touch the card.
- 2026-09-26 22:37:55, live: 「帮我给我。」 put up a letter card addressed to
  Allen himself. 1.4 s later 「给我的朋友写一份邮件」 dropped that turn's answer,
  but the card stayed, and the next turn's note showed it with its arguments.
  Allen pressed × on it at 22:38:06, 4.7 s after it should have gone.
- Allen, same night: 「应该是撤掉，然后再重新发」 — the card goes with the
  dropped answer, and the next turn asks again.
- `answer_confirmation_once` is the one writer of a card's answer: one answer
  per ask, re-read under the write lock. A button that loses the race to it
  finds the card already answered, as it does for any card that is gone.

## Decision

While any of Allen's utterances is in flight (from speech onset until it is
accepted or comes to nothing), no response run completes and no answer starts
playing. When one is accepted, before its `utterance.received` is written,
each open run of the last 10 s is cancelled with reason `superseded` and its
queued audio discarded if all of these hold:

- it answers another of his voice sentences or a filled-in ask card;
- none of its answer has reached the speaker.

A card such a run put up that still waits is rejected with rule
`superseded`, so the new turn sees no card and asks again.

Limits:

- Only runs whose interrupt policy lets generation be cancelled.
- A turn any of whose audio has played is never dropped (barge-in owns it).
- Typed text and card buttons are never dropped.
- A hold that is never released delays completion by at most 60 s.
- A card is withdrawn only when its run's cancel lands; a run that completed
  first keeps its answer and its card.

## Alternatives rejected

- **Leave the card (ADR 0072 as written)** — the 22:37 run above: a letter to
  the wrong person waited for a send button for 4.7 s, and the next turn's
  note fed its arguments back to the model.
- **Close it with `confirmation.expired`** — the answer path, the dispatch
  revalidation and the card route fold only `requested`, `accepted`,
  `rejected` and `gate.evaluated`, so a send pressed after the expiry would
  still be accepted and dispatched. ADR 0062 also says a card never expires.
- **Hide the card on the surface while Allen talks** — the card still waits
  in the log, the next turn's note still shows it, and its button still sends.
- **Voice sentences only (ADR 0053)** — on 2026-09-26 22:13 a stale question
  to a filled-in card played for 4 s after Allen had answered it.
- **A longer pause before a turn ends** (`required_misses` 24 → 32, 0.77 →
  1.02 s) — every answer starts 0.25 s later, and a hesitation longer than the
  new pause still splits the sentence the same way.
- **Keep the dropped answer in memory.db, marked unspoken** — the next turn's
  history then shows Jarvis asking a question, and the model reads Allen's
  next sentence as a reply to it (Allen's pick, 2026-09-25).

## Consequences

- Every answer, a background report included, waits while Allen talks, for up
  to the utterance's 30 s cap.
- Within 10 s, a new sentence cancels a tool turn Allen just asked for,
  whether he asked by voice or on a card. Actions it already dispatched are not
  undone. The new turn's prompt carries both, so the model may dispatch them
  again.
- The withdrawn card's `confirmation.rejected` row is attributed to Allen
  (`actor` user, no words): his next sentence is what withdrew it.
- A dropped turn's card that had replaced an earlier waiting card leaves no
  card; the earlier one does not come back.
- The card shows from the moment it is asked, so a wrong one can appear for
  the rest of Allen's sentence and until the surface's next read.
- A card turn's status line about blank fields is lost when that turn is
  dropped; the next turn gets Allen's answers and his new sentence without it.
- A streamed answer's text reaches the surfaces before its run completes, so
  a dropped one's words may already be on the wire. Surfaces must not show
  answer text while Allen's words are in flight.
- A run that passed the completion hold just before Allen started talking can
  complete between the drop and its cancel. Its queued audio is then lost
  while its row stays. The window is milliseconds wide.
