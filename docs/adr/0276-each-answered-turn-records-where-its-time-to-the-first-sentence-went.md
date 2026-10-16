# ADR 0276 — Each answered turn records where its time to the first sentence went

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** none

## Context

- The first sentence of an answer left at about 1.1 s after his words on gpt-6-luna with
  the priority tier (2026-10-05 to 10-10) and at about 2.1 s on Claude Sonnet 5.5 with adaptive
  thinking at effort low (since about 10-12). The prompt grew from 16 to 28k tokens to about
  75k at the same time (ADR 0258). Nothing on record says which segment grew: the context
  blocks, the request reaching the model, the model reading the prompt (a cache miss), the
  thinking before the text, or a hold in front of the first chunk.
- The brain runs on a Pi (ADR 0170) and the Mac can only make authenticated HTTP reads of it.
  What exists is in the wrong place for this: `realtime_trace` points sit in process memory
  (or an opt-in JSONL on the Pi), `cost.recorded` has tokens and cost but no time, and the
  response rows time the answer's end, not its beginning.
- The causes are mixed. The holds in front of the first chunk are four separate mechanisms
  (ADR 0053, 0263, 0272 and Jev's wait for the model's first output, ADR 0122), so a total
  alone cannot name which one delayed a turn.
- Only one process holds the whole turn, so its monotonic clock is the only one that needs
  no correction: the machines' clocks need not agree.

## Decision

The brain writes one `turn.timing` event for each turn it answers, from marks on its own
monotonic clock, and serves them at an authenticated read-only `GET /inherent/turn-timings`.

- **What is marked.** Where the turn's time went before the model (the queue, the context
  blocks by name), each model request (made, sent, first stream event, first thinking, first
  text, finished, its usage including the cache read and write tokens, and whether it ended in
  a call), the first chunk that left her, and each hold that delayed that chunk, named by the
  ADR that makes it. The detail is in the event schema and `docs/spec.html`.
- **Where.** The runtime binds one timer to the turn's thread; the code that already knows each
  moment marks it. Marks are clock reads; nothing waits on them and no answer changes.
- **Failure.** A measurement never fails a turn: if the event cannot be written the turn is
  answered and the loss is logged.
- **Not covered.** A turn that is cancelled or fails is not an answered turn and writes no
  fact.

## Alternatives rejected

- **Read the in-memory realtime trace over a new route.** It is bounded to the last 4096 points
  of one process, is empty after the Pi's restarts that the regression spans, and carries no
  usage or cache tokens. A fact in the log survives restarts and joins `cost.recorded` by
  request id.
- **Extend `cost.recorded` with the times.** One row per request, written when the request
  settles; the turn-level moments (context, first chunk, the holds) belong to no request.
- **Time only the model request.** Misses the segments that differ between the two eras
  without being the model: the 75k-token context build, and the 1.5 s answer hold of ADR 0272
  (2026-10-14, inside the regression window), which alone can add to a turn what the model did not.
- **Ship the Pi's JSONL trace to the Mac.** Needs ssh or a second transport, which is the thing
  this avoids, and the file has no per-turn join.

## Consequences

- One extra event row per answered turn (about 1.5 KB). It sits in the event log that the
  export and the day line already read, so `turn.timing` is also data the owner's export
  carries.
- A tool-using turn that waits for L4 across a suspension keeps one clock, so its tool time
  is inside `first_text_ms`; the fact says a tool round came first and the segments of the
  request that answered separate it.
- The thinking is counted in bytes of text, not tokens: Anthropic reports no thinking token
  count apart from the output total.
- Marks inside `decide()` make that module's timing visible to a future reader of it; a new
  request path has to mark itself or its time is missing from the fact, not wrong.
