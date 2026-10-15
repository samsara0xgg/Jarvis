# ADR 0268 — A sentence that follows an unspoken one answers both, and the log names the turn that took over

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** 0074

## Context

- Allen's rule, 2026-09-25 (ADR 0053): she does not talk while he talks. When he
  stops with a real sentence, her unspoken answer to the one before it is dropped
  and both are answered once; once she is audible, barge-in owns the turn.
  ADR 0074 built it: while any of his utterances is in flight no run completes;
  when one is accepted, before its `utterance.received` is written, each open
  run of the last 10 s that answers a voice sentence or a filled-in card, whose
  policy lets generation be cancelled and whose audio never reached the speaker,
  is cancelled as `superseded`, and a card it put up is rejected. A turn whose
  run has not opened yet is marked and cancelled as its run opens. The new turn's
  request already carries both sentences, because every turn's words are
  appended to memory.db at its start and a history that ends on unanswered
  `allen` rows folds them into the next user message (ADR 0044).
- Checked 2026-10-14 against the shipped code, two spoken sentences one second
  apart, the first request in flight with no word out: the first run is cancelled
  quietly, one request is answered and holds both sentences in order, one answer is
  rendered, and memory.db holds both of his rows and one of hers.
- Live 2026-10-14 21:18 PT, a Mac voice terminal, four different sentences in one
  session: two turns were cancelled right after they began, a third answered a bus
  question, a fourth asked it again. One of the cancels was the companion's exit
  (ADR 0269). The rest of the log could not say whether a `superseded` cancel had
  been merged into a later turn or had simply lost his words: `response.cancelled`
  has an `interrupted_by_turn_id` field, declared in its schema, that nothing writes.
- Gaps between the halves of one sentence in the record: 1.4 s (ADR 0074's card
  incident), 1.7 s (ADR 0053), 2 s and 3 s (ADR 0219). A weekday question 7 s after a
  search began was a new question, not a half (ADR 0107's live test).
- Live 2026-10-14 22:14:19 and 22:14:24, the same sentence twice from one Mac voice
  session: the first turn had called `where_am_i`, so the sweep left it alone (ADR 0107)
  and the second turn started a second answer. ADR 0219 absorbs a repeat only across
  devices.
- A run that completed has its row in memory.db, which is append-only, so a
  completed answer cannot be taken back (ADR 0053).

## Decision

Name the turn that takes a superseded turn's place: every cancel of a run as
`superseded` writes the new turn's id in `interrupted_by_turn_id` of its
`response.cancelled`, and the rule that decides what is superseded stays as ADR 0074
set it, restated here.

While any of his utterances is in flight, no response run completes and no answer
starts playing. When one is accepted, before its `utterance.received` is written,
each open run of the last 10 s is cancelled with reason `superseded`, its queued audio
discarded, if all of these hold:

- it answers another of his voice sentences or a filled-in ask card;
- none of its answer has reached the speaker.

The name is written wherever a run is superseded: by that sweep, by the cancel at the
open of a run that was marked first, and by Jev's supplement or correction (ADR 0139).

A repeat starts no answer. When a voice turn begins, its words are compared with those
of the earlier voice turns of the same device (`ingestion_node`) that began in the last
10 s or still have an open run, and that were not cancelled or stopped. The comparison
is `device_voice`'s, ADR 0219: punctuation, whitespace and a wake lead are ignored;
in addition a listening sound (嗯, 啊, ...) at either edge is ignored. If the new words
are the same as the earlier turn's, or a part of them (at least 2 characters), the new
turn is a repeat of the latest such turn: his row is written as for any turn, the turn
is marked superseded by that one, and its run is cancelled as `superseded` the moment it
opens, with `interrupted_by_turn_id` naming the turn that answers for it. No request is
sent for it. This holds whether or not the earlier turn dispatched an action, because
the exemption protects a turn from being cancelled, and here the earlier turn is kept.
Longer words that only begin with the earlier turn's are a continuation and follow the
rules above, as do different words.

Limits:

- Only runs whose interrupt policy lets generation be cancelled, and, with
  `slow_results` on, not a turn that already dispatched an action (ADR 0107).
- A turn any of whose audio has played is never superseded: barge-in owns it.
- Typed text and card buttons are never superseded.
- A card a superseded run put up that still waits is rejected with rule `superseded`,
  only when the cancel lands.
- A device's sentence supersedes that device's turns only (ADR 0219).
- A hold that is never released delays completion by at most 60 s.

## Alternatives rejected

- **Hold every voice request a few seconds so one request carries both** — every
  spoken answer starts that many seconds later; the cancel path wastes one request
  only when a second sentence comes, and ADR 0074 already rejected a longer pause
  for the same reason.
- **A `turn.merged` event** — `response.cancelled` already declares the field, and a
  second row would state the same fact twice and need an event type.
- **Merge into one stored record** — memory.db is append-only and its rows anchor
  summaries and the Conversation page cursor (ADR 0053); his two rows already read as
  one message in the request.
- **Narrow the window to 3-5 s** — the record has no merge that was wrong at 10 s and
  no miss to fix; a shorter window would move the line without a failing case.
- **Cancel the earlier turn when a repeat arrives** — for a turn that dispatched a tool
  that discards work that is already running (ADR 0107); the repeat is the one with
  nothing to lose.
- **Supersede a turn that dispatched an action** — the 2026-10-01 live test: a
  weekday question 7 s after a Micron search cancelled it, re-ran it for 24 s and never
  answered the weekday.

## Consequences

- A request cancelled in flight is paid for and thrown away; only the merged turn's
  answer counts.
- A turn that dispatched a tool is outside the merge: two different sentences get two
  answers, as before. The same words again do not.
- A repeat within 10 s of an answered turn is swallowed too, so asking the very same
  words again that quickly gets no second answer; the first one is the answer. A longer
  sentence, or the same one after the window, is answered.
- A continuation that is longer than the earlier words, such as a cumulative transcript,
  is not absorbed: it still follows the merge or the exemption.
- The cancel of the first sentence still reaches the surfaces as `cancelled` for that
  turn; whether the screen shows it is the surface's.
- A run that passed the completion hold just before he started talking can complete
  between the drop and its cancel; its queued audio is then lost while its row stays.
- A card turn's status line about blank fields is lost when that turn is superseded.
