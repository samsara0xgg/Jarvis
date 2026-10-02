# ADR 0128 — Every Jev call is kept on Allen's disk as training data

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Allen, 2026-10-02, wants every Jev decision kept locally so a small model of
  his own can be trained to answer what Jev answers: the hosted model is paid
  per call and bounded by ADR 0122's 0.4 s and ADR 0123's and 0125's 1.5 s
  deadlines, and a local one would have neither.
- Jev is reached through one transport, `SurrogateRoute.post`, by three callers
  (the instant route, the mail marks, the turn-end asks). What they record today
  cannot train anything: the instant route's `route.surrogate_decided` event
  holds the choice, the confidence and the cost but not the words Jev read; the
  mail and turn-end callers log an id, a probability and a cost to the daemon
  log, which is rotated and by ADR 0123 and 0125 carries no text.
- Calls end on worker threads, and a decision may be waiting on one, so the
  record cannot cost the decision time or fail it.
- The words are Allen's own speech, the senders and subjects of his mail and
  the tail of his agents' messages. They already leave the Mac for Jev; a copy
  that stays on it adds no egress. A file kept for a model that does not exist
  yet is also a file nobody will remember to turn on afterwards.

## Decision

Append one JSON line per ended Jev call, with exactly what was sent and the raw
answer, to `jev/decisions.jsonl` under the runtime root, at the one transport
every Jev call goes through; keep it on whenever a Jev feature is on, and let
`jev_log.enabled: false` turn it off.

- **One writer, at the transport.** A call line is written when the request
  ends, answered or failed, with the failure kind in place of the answer: a
  late answer the caller already gave up on is still logged with what Jev
  eventually said. The line is made from the request body and the decoded
  reply, never from the headers, so the key cannot reach it. A call that was
  never sent (no key) writes nothing.
- **Tagged to join.** Each caller names its `use` (`route`, `mail`,
  `turn_end`) and a `ref`: the turn id for the route, which joins the line to
  that call's `route.surrogate_decided` event and the action rows of the same
  turn; the Gmail id for a mail letter; the session id for a turn ending.
- **Decisions and outcomes are small lines in the same file.** The mail and
  turn-end callers append what they made of the answer (the reply mark and the
  junk flag; asks or not), because the bars they apply live in config and
  change. The route needs none: its event holds `accepted`. Allen's archive and
  undo of a junk letter, taps `Home.archive` already sees, append an outcome
  line keyed by the letter's id: an archive agrees with the junk offer and an
  undo says it was wrong.
- **Never in the way.** The write runs on the thread that ended the call, after
  the call's finish time is taken (so no deadline includes it), under one lock
  with one append per line; an append costs about 60 microseconds. A failed
  write is logged once and the line is dropped; no decision reads the file.
- **Private, local, uncut.** The file is mode 0600 in a 0700 folder made at the
  first line, never leaves the Mac and is not rotated. Lines are 1.3 to 1.7 KB:
  a hundred calls a day is about 55 MB a year.
- **Outcome signals that exist:** the route's `accepted` and `aborted` and the
  Tier 0 action of that turn (in the event log, by turn id); a mail archive or
  undo (this file).
- **Outcome signals still missing:** no control anywhere says "Jev was wrong";
  a function the route ran that Allen cancels or rephrases at once is not tied
  to the call; whether Allen replied to a letter marked `yes`, or ignored a junk
  offer, is not observed; the board sees Allen open or answer a finish told as
  "needs you" but does not log it; a finish Jev did not flag that turned out to
  ask, and every `fyi` mark, have no signal at all.

## Alternatives rejected

- **Widen the `route.surrogate_decided` event with the text** — it covers one of
  the three callers, and the mail and turn-end calls run on worker threads that
  write no events (ADR 0123, 0125); putting every sender and subject into the
  event log would also put them in the store ADR 0067's export copies and
  nothing prunes.
- **Reuse `diagnostics.log_llm_io` (ADR 0120)** — it records the OpenAI model's
  requests, not Jev's, and is off by default as a test-time record; a dataset
  that starts only when someone remembers to switch it on is missing the weeks
  that would have trained it.
- **A queue and a writer thread, as `llm_io_log` has** — that exists so a
  stream's first word never waits on disk; here the append is 60 microseconds on
  a worker already off the decision, and a queue adds lines that can be lost at
  shutdown and something to flush.
- **Log only the calls the callers acted on** — a model trained on accepted
  choices alone never sees a `none`, a below-the-bar answer or a failure, so it
  cannot learn to hold back, which is the half of Jev's job the 0.95 bar exists
  for.
- **One key per feature** — three keys for one file and one choice of Allen's;
  the writer is shared, so the switch is.

## Consequences

- Allen's speech, his mail's senders and subjects and his agents' last
  paragraphs accumulate in one plaintext file that nothing deletes; "erase all
  data" removes it with the rest of the runtime root and the export does not
  include it. The spec's list of what is kept on disk says so.
- The question text (about 0.6 to 1.2 KB of each line) is stored again on every
  line; if the file grows past what Allen wants, deduplicating it is a change to
  this ADR's format, not a reason to cut what Jev saw.
- The turn-end decision line is written by the call's done callback, so it can
  land a moment after the call line and, for a turn ending, is the only line
  keyed by a session id that repeats across turns; a reader pairs each decision
  with the call line before it.
- A route call that was held back for the model (ADR 0122's parallel path) and a
  call the deadline cut are both logged with what Jev said, while their event
  says `accepted: false`; a reader must take "was acted on" from the event, not
  from the answer.
- A change to a question's wording changes the data silently; the route's
  `OPTIONS_VERSION` is in the event, and the mail and turn-end questions are in
  every line.
