# ADR 0278 — A sentence spoken before her answer is over goes on from the sentence before it

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** 0271

## Context

- Live 2026-10-15 15:22, Mac voice terminal: Allen asked how to get to an office, and while
  she was still answering he added two sentences about 1 s apart. Each became its own turn.
  The pre-emit sweep (ADR 0268) cancels only an unspoken answer and, with `slow_results` on,
  spares a turn that already dispatched a tool (ADR 0107), so two turns ran at once, each
  searched, and their answers contradicted each other.
- The phone already composes a continuation into one turn (ADR 0271): the brain cancels the
  earlier turn naming the new one and writes the whole thought as the new turn's transcript.
  It does so only when the phone names the earlier sentence, which the phone decides by a 4 s
  client-side window. The Mac was left unchanged.
- No time window is the right test. Of 269 historical cases where he spoke before her answer
  to his previous sentence ended, merging was right or harmless in 254. Continuations,
  corrections and repeats spread out to 15 s and beyond; only half were within 4 s, so any
  cut-off drops half of them. The harms are two: an unrelated new request is answered jointly
  (nothing is lost), and a stop word is folded into a request.
- What separates the cases is state: whether her answer to the earlier sentence is over. An
  answer that completed is a record that cannot be taken back (ADR 0053); one that has not
  is still his thought.
- A turn that called tools has done work. The composed request cannot replay it as tool
  blocks: the provider requires the model's thinking blocks with them, which a cancelled
  turn's log does not hold.
- A turn that never ends (a lost run) must not capture every later sentence for ever.

## Decision

The brain composes a spoken sentence from any device into that device's latest earlier spoken
turn whenever her answer to that turn is not over and it began less than 120 s ago.

"Not over" is the reading ADR 0271 already uses: no `turn.ended` or `turn.failed` for the
earlier turn, and its final run has not completed, been cancelled or failed. The 120 s is a
bound for a turn that never ended, not a heuristic about pauses.

The new sentence is composed as ADR 0271 composes: the transcript is the whole thought, the
segment is the new words, the earlier turn is cancelled as `superseded` naming the new one
whether or not it dispatched a tool and whether or not any of it is audible, its unspoken
audio is dropped, audible playback on that device is stopped, and a card it put up that still
waits is withdrawn. A chain composes into the latest turn, and an earlier turn that a later
sentence took over, or that was cancelled as a repeat, is looked past.

The composed request carries a note listing each tool call the folded turns made and the
result they observed (each cut at 4000 characters), saying they were done for this thought,
so she uses them instead of calling again.

Limits:

- Typed words never compose. A stop request is never composed. Words that say no more than
  the earlier turn did stay with the repeat rule of ADR 0268: his row is kept, no second
  request starts.
- Earlier turn over, unknown, not a spoken turn of this device, or a host without run
  bookkeeping: the new words stand alone.
- The Mac's sweep leaves the device's latest pending spoken turn to composition and cancels
  every other turn as before. The host's own session, which records its utterances itself,
  keeps the sweep alone.
- The phone's `continues`, when sent, is accepted if it names the turn the brain would pick
  and dropped otherwise; without it the brain composes by state.

## Alternatives rejected

- **A longer time window, on the phone or the brain** — of 269 historical cases only half of
  the continuations, corrections and repeats were within 4 s and the rest reached 15 s and
  beyond, so a window of any length either drops half of them or captures unrelated ones with
  no state to tell them apart.
- **Keep the tool exemption of the sweep** — the 15:22 incident is exactly a turn that had
  dispatched a tool: two searches ran and contradicted each other. A cancelled turn's tool
  work is carried as a note, so cancelling it re-runs nothing.
- **Replay the earlier tool calls as `tool_use` and `tool_result` blocks** — Anthropic
  requires the model's thinking blocks with them; a turn cancelled mid-request has not
  written those, so the request would be refused.
- **Ask the terminal to name the earlier sentence, as the phone does** — it would need a new
  field on a protocol whose terminal ships separately, and the brain alone holds the turns and
  their runs; the choice is a state read the brain already makes for the phone.
- **A completeness judge before composing** — it answers whether a sentence is finished, not
  whether the next one belongs to it; 254 of 269 cases were right or harmless without one.

## Consequences

- An unrelated new request spoken before her answer ends is answered jointly with the earlier
  one; nothing is lost, but it gets one answer, not two.
- A stop word that reaches the recorded utterance without being judged a stop would be folded
  in; the judges and `is_stop_request` stop the cases seen so far.
- The first turn's request, and any tool call it made, is paid for and discarded or reused
  only through the note. A call still running when the composed request is built shows as
  having no result yet, and the model may call it again.
- Only the earlier turn whose words repeat the new ones is checked for a repeat; a repeat of
  an older turn behind a newer pending one is composed with the newer turn.
- The host's own session (no terminal) keeps the old sweep and its tool exemption.
- A terminal's `utterance` ask now waits for the drop, the stop and the cancel before the row
  is written, up to the terminal's command timeouts.
