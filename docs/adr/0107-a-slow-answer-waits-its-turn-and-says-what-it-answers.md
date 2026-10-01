# ADR 0107 — A Slow Answer Waits Its Turn and Says What It Answers

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- 2026-09-29 live (handoff §3.1, 轮次并发): the email lookup took over 30 s.
  Allen asked something else meanwhile, and the new turn ran beside the email
  turn. It saw the email question unanswered, looked the email up again, and
  its line before the call cut off the email answer as it began. His other
  question went unanswered. His ask: answer what he just said first; when
  the slow result is back, wait until she has finished, then say it,
  starting like 「对了，邮件查到了」.
- The newer turn's history ends on the unanswered question, and its state
  block said `Previous turn: interrupted before it was answered` about a
  turn that was still running. Nothing told it another turn had the
  question.
- The speech lane (ADR-0008 D8, `decide_foreground`) orders two turns'
  answers by the row of each answer's `surface.response_open`, which is
  written when the answer's first sentence is, not when the question was
  asked. The later opener cuts the one playing; one that opened first but
  finished while another plays is declined and never said. So a slow
  answer cuts off the answer to the newer question, or the newer turn's
  line cuts off the slow answer: whichever started speaking later wins.
- ADR 0074 still covers the short case: within 10 s of a question, with
  nothing of its answer heard, a new sentence cancels that turn and both
  are answered as one. A lookup past 10 s, or one whose line was already
  said, keeps running. With this flag on, so does one that has already
  dispatched a tool, however recently he asked (live test 2026-10-01: a
  weekday question 7 s after a search started cancelled it, and the
  weekday was never answered).

## Decision

Behind `realtime.response.slow_results.enabled` (on by default since Allen accepted this on 2026-10-01, after the live test), when
another of Allen's turns is still running:

1. The new turn's state block names each earlier question another turn is
   still answering, with how long ago it was asked, and tells the model to
   answer only the new words and not to answer or look up the earlier one
   again. The look-back for where the last answer was cut passes over those
   turns. A turn counts as running when it started from Allen's words before
   this trigger, within the last 5 minutes, and has no `turn.ended`,
   `turn.failed` or `response.cancelled`.
2. After a tool round, when Allen has said or typed anything since this
   turn's own words, one runtime note follows the tool results, once per
   turn: what he said meanwhile, that it is answered separately, and that
   this answer opens with a few words pointing back to its question, in his
   language.
3. On the speech lane, another turn's answer waits until the one playing has
   finished, whichever turn is newer: none cuts another off, and none is
   declined for arriving second. The playing answer's own next part (its
   answer after its line) goes ahead of another turn's waiting answer.

Its limits:

- A stop, a barge-in, or L3 ending the playing answer still clears
  everything waiting, the slow answer included. It stays on screen; with
  the unspoken-answer proposal, the next turn is told it was never spoken.
- Saying 「算了」 does not stop the earlier turn: no tool can, so its answer
  still comes. Stopping it while it speaks does.
- Apart from that one rule, waiting answers play in the order they arrive,
  so a short answer can wait behind a long one.
- The lane still holds 8 waiting answers; past that, one is dropped as
  before.

## Alternatives rejected

- **Order the lane by when each question was asked, newest cuts in.** The
  09-29 cut was exactly this: the newer turn's line cutting off the email
  answer that had begun. It also needs L5 to learn each turn's question row,
  which no media event carries.
- **Keep declining the older answer and rely on the screen.** The slow
  answer is the one he waited 30 s for; declined, he never hears it.
- **Cancel the older turn when he speaks again, past ADR 0074's 10 s.** It
  throws away a lookup he still wants, and he asked for it to come after.
- **Tell the new turn about the earlier question and let it answer both.**
  On 09-29 it did, and looked the email up a second time.
- **Put a fixed 「对了，」 in front of a late answer.** It cannot name what the
  answer is about (「邮件查到了」), and the model already knows the question
  and his language.

## Consequences

- With the switch on, each turn runs one Event Log read at its start and one
  after each tool round.
- How the late answer opens is the model's wording; it needs a live check.
- ADR 0044's state block gains one line and the request one more note, and
  ADR-0008 D8's cross-group take-or-decline becomes wait, while the switch
  is on.
- `tests/integration/test_slow_results.py` drives the media owner with each
  policy (the live cut and the lost answer with it off, both said in order
  with it on) and two real `drive_turn` calls at once against a localhost
  Responses peer.
