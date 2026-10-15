# ADR 0271 — A phone's sentence that goes on from its last is composed by the brain into one turn

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** none

## Context

- Live 2026-10-14 21:36 PT, phone voice on a bus: one long thought was answered eight times,
  each request carrying a longer copy of the same words. Two things on the phone caused the
  copies. It ends an utterance after 0.8 s of unchanged recognizer text and a quiet
  microphone, with no test that the sentence is complete. And, since 2026-10-13, it joined
  his words over her onto his last sentence when they began within 4 s of it (for
  「我现在在。」|「往39路的地方走」), sent the joined text as a new `say`, and kept the joined
  text as the base of the next join, so every join was longer than the one before.
- The brain takes every `say` as a new turn (ADR 0209). With `over_her` it stops the phone's
  audio and cancels the generation of the one open answer, if there is exactly one and its
  policy allows (ADR 0216, 0100); it names no successor, and the phone path never calls the
  sweep of ADR 0074 or the hold of ADR 0053. The earlier turn's row stays (memory.db is append-only and its rows
  anchor summaries, ADR 0053), and a history that ends on an unanswered row of his folds it
  in ahead of the next request (ADR 0044), so a joined text put the thought into the request
  twice.
- ADR 0268 names the turn that takes a superseded turn's place and drops an unspoken answer
  when the next sentence arrives; an answer that has begun is barge-in's (ADR 0053, 0100). It
  decides from text and timing alone, so it cannot tell a sentence he is still saying from a
  new request. The phone can: it knows when his words started relative to his last.
- Two owners of a join cannot both be right. The phone's join is invisible to the brain's
  records, runs and audio; the brain alone holds the turns, their runs and their sound.
- A completeness test before she starts answering (an offline eval is running) is a separate
  decision; this one only fixes who composes a thought that the phone already knows is one.

## Decision

A phone's spoken `say` names the earlier spoken `say` of its own that it goes on from and
carries only its new words; the brain composes the thought into one turn.

The new turn's words are the earlier sentence's whole thought followed by the new words. The
earlier turn is cancelled as `superseded` naming the new turn (ADR 0268), its unspoken audio
dropped and what was audible stopped, whether or not any of it has reached the speaker. The
phone keeps holding her voice while he speaks over her (ADR 0216); a judged `say` takes over
from the earlier one only on a `turn` verdict.

How the log and the history hold it:

- The event of the new turn holds the thought as its transcript, the new words alone as
  its segment, and the earlier turn and the record ids whose words the transcript holds.
  A third sentence names the second, whose transcript already holds the first, so a chain
  needs only the last id.
- Every segment is its own memory.db row, as spoken. The new turn's prompt reads the thought
  from the event and leaves the rows it holds out of its history; a later history reads the
  adjacent rows as one message, so the thought is there once. No sent message is kept for a
  composed turn, since replaying it beside the rows would repeat the first words.

Limits:

- Unlike the sweep, this holds for an answer that has begun to speak and for a turn that
  dispatched a tool: the phone has said what these words are (compare ADR 0107).
- When the earlier turn is over (ended, failed, or its final answer completed, cancelled or
  failed), is unknown, or is not a spoken `say` of this device, the new words stand alone
  as a turn and the name is dropped. A completed answer cannot be taken back (ADR 0053).
- Typed words never continue. A resent `say` is not taken over twice. A card the earlier
  turn put up that still waits is rejected, as in ADR 0074 (ADR 0072).
- A phone that does not send the name, or a brain that does not know it, behaves as before.
- The Mac is unchanged. It already sends only the new sentence, and its request folds the
  earlier unanswered row in once (ADR 0044). Whether a resumed sentence continues the last
  or asks something new is not known there without the completeness judge; until then a
  resumed sentence over her audible answer stays barge-in's, as ADR 0268 left it.

## Alternatives rejected

- **Keep the join on the phone and let the brain notice a text that begins with an earlier
  turn's words** — the recognizer revises earlier words as it goes (the phone's own
  growing-partial test is "give or take a revision"), so a prefix match can miss, and the
  brain would be guessing at what the phone knows.
- **Run the ADR 0074 sweep for the phone's says** — it cannot tell a continuation from a new
  request, and it leaves an answer that has begun to barge-in, which names no successor.
- **Store the composed text as the new row and mark the earlier row as continued, skipped in
  the history** — the records table has some twenty `SELECT`s over it (the conversation page, daily
  reports, search, summaries, the meaning index); each would need the filter, and one that
  missed it would show the thought twice or lose words. Rows as spoken need none.
- **Send only the new words and rely on the prompt fold of ADR 0044** — the earlier turn's
  answer would not be cancelled while it thinks or speaks, and Jev, the tool predictor and
  the words judge would read a fragment such as 「往39路的地方走」 as the whole request.
- **Cancel the earlier turn with the phone's barge-in** — it cancels only when exactly one
  final run is open, answers `ambiguous_open_runs` otherwise, and names no successor.

## Consequences

- A request already sent for the earlier turn is paid for and thrown away; an action it
  dispatched loses its turn, as under the sweep with `slow_results` off.
- The first turn still starts answering during a pause until the second segment lands;
  shortening that is the completeness judge's job.
- A reader of `utterance.received` that wants what was newly said reads the segment; the
  transcript is the thought.
- A composed turn replays from its rows, so it adds no provider-cache prefix of its own.
- A brain without a run registry cannot cancel; the words then stand alone.
- The phone shows only the new segment as his words while the brain's rows show each one;
  the thought appears whole in the history.
