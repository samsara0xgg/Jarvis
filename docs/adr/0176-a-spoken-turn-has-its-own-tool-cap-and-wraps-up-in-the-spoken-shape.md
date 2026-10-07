# ADR 0176 — A spoken turn has its own tool cap and wraps up in the spoken shape

**Status:** Accepted
**Date:** 2026-10-07
**Supersedes:** 0060

## Context

- ADR 0060 sets one bound, `llm.max_tool_iterations`, for every turn, and
  Allen's config raised it to 40 for a typed hotel booking that took 16 to 18
  requests.
- A voice turn is silent while it loops: the answer is spoken only when it is
  complete. On 2026-10-07 a broad mail question ran 42 model requests over two
  minutes before the bound ended it.
- Since 2026-09-25, 5 of about 780 turns used more than 8 decision requests.
- A spoken turn's normal requests carry the `spoken_reply` schema (ADR 0114):
  one or two sentences in `spoken`, the details in `written`. The request after
  the bound asked for prose, so a wrap-up on that route would have been read
  out whole or shown twice.
- Allen, 2026-10-07: voice turns get a lower cap; the wrap-up must have the
  shape of a normal turn of its channel.

## Decision

Bound a turn Allen spoke by `llm.max_tool_iterations_voice` (8 in his config;
the same as `llm.max_tool_iterations` when unset) and every other turn by
`llm.max_tool_iterations`; the request after the bound goes through the turn's
own request builder, so a structured spoken turn gets the `spoken_reply`
schema and a note that asks for what was established and what could not be
finished in `spoken` and the details in `written`.

## Alternatives rejected

- **One lower cap for every turn.** The booking turns that motivated ADR 0060
  need 16 to 18 requests; 8 would end them at the results page.
- **A time limit on voice turns instead of a count.** Requests take 2 to 3
  seconds each but a tool such as the browser can take far longer, so a clock
  would cut a turn that is making progress and let a fast loop run to 40.

## Consequences

- A spoken turn that needs more than 8 requests ends with a partial answer
  and Allen says "continue"; a long spoken booking no longer finishes in one
  turn.
- A typed turn that loops still runs to 40 and answers in one prose reply,
  which the surface shows whole and, on a voice surface, rewrites for speech.
