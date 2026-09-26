# ADR 0053 — Jarvis Waits While Allen Talks and Answers a Split Sentence Once

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** none

## Context

- In conversation mode (ADR 0041) a pause of 0.77 s ends an utterance once it
  holds 0.3 s of voiced speech (`min_voiced_s`, lowered from 1.0 on
  2026-09-25 so that a short question ends at its pause). A sentence with a
  pause in the middle becomes two utterances: on 2026-09-25 22:30 "怎么说……到一半停了"
  arrived as "怎么说。" and "怕说到一半停了。", 1.7 s apart.
- Each utterance is its own turn. Turns run concurrently (two workers) and
  nothing cancels an earlier one. The first half's answer is ready 2-3 s
  after it, usually while Allen is still saying the second half. Barge-in
  fires only at speech onset while Jarvis is audible, so that answer starts
  over him; the second half's answer then cuts it off in the media lane.
- The second half's prompt already carries the first half: a history that
  ends on an unanswered `allen` row is folded into the new user message
  (ADR 0044). The first answer is the only thing wrong.
- `min_voiced_s` cannot tell a fragment from a whole short question: "你怎么"
  and "你是谁" both hold about 0.5 s of voiced speech.
- ADR-0008 D1: a run is terminal at generation completion, and memory.db
  gets Jarvis's row right after render. After that point an answer cannot be
  taken back without rewriting memory.db, which is append-only.
- ADR-0008 D10: runtime applies the run's own interrupt policy; `superseded`
  is in the closed cancel vocabulary.
- Allen, 2026-09-25: she does not start talking while he is talking; when he
  stops with a real sentence, her unspoken answer to his previous sentence is
  dropped and both halves are answered as one; if his sound was nothing, the
  waiting answer plays; once she is audible, barge-in stays as it is. A
  dropped answer does not exist: not heard, not in the record, not in the
  next turn's history.

## Decision

While any of Allen's utterances is in flight (from speech onset until it is
accepted or comes to nothing), no response run completes and no answer starts
playing; when one is accepted, before its `utterance.received` is written,
every open run answering another voice sentence of the last 10 s whose answer
never reached the speaker is cancelled with reason `superseded` and its queued
audio discarded.

Limits: only runs whose interrupt policy lets generation be cancelled; a turn
any of whose audio has played is never dropped (barge-in owns it); a hold that
is never released delays completion by at most 60 s.

## Alternatives rejected

- **A longer pause before a turn ends** (`required_misses` 24 → 32, 0.77 →
  1.02 s) — every answer starts 0.25 s later, and a hesitation longer than the
  new pause still splits the sentence the same way.
- **`min_voiced_s` back to 1.0** — on 2026-09-24, 5 of 7 short questions
  ("你是谁", 0.3-0.8 s voiced) never ended at their pause and waited 4-7 s for
  Allen to speak again (commit 2fb27ca).
- **A semantic endpoint (ADR-0006 D7 partial ASR)** — built but off and never
  calibrated; it decodes every 240 ms while Allen speaks, and "怎么说" reads as a
  complete sentence, so it would end the same fragment.
- **Keep the dropped answer in memory.db, marked unspoken** — the next turn's
  history then shows Jarvis asking "你想让我怎么说？", and the model reads the
  second half as Allen's reply to that question (Allen's pick, 2026-09-25).
- **Delete the dropped answer's memory.db row** — memory.db is append-only and
  its rows anchor summaries and the Conversation page's cursor; holding
  completion keeps the row from being written instead.
- **Drop in the intent pump when the new turn arrives** — the pump can start
  the new turn before the drop lands, so the new prompt could still carry the
  dropped answer; dropping before `utterance.received` is written orders it
  ahead of the new turn.

## Consequences

- Every answer, a background report included, waits while Allen talks, for up
  to the utterance's 30 s cap.
- Within 10 s, a new sentence cancels a tool turn Allen just asked for. Actions
  it already dispatched are not undone; the new turn's prompt carries both
  sentences, so the model may dispatch them again.
- A streamed answer's text reaches the surfaces before its run completes, so
  a dropped one's words may already be on the wire; surfaces must not show
  answer text while Allen's words are in flight.
- A run that passed the completion hold just before Allen started talking can
  complete between the drop and its cancel: its queued audio is lost while its
  row stays. A window of milliseconds.
