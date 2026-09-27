# ADR 0060 — The tool budget is a config value

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** none

## Context

- ADR 0030 bounds a Tier 2 turn at 5 tool-proposing model requests. The
  bound caps the cost of a model that never converges. It rejected raising
  the bound for a turn that needed a sixth request to read its fifth result.
- ADR 0059's browser works in small steps. The first live booking turn spent
  all 5 requests on a tool search, opening Booking.com, finding the search
  box, loading the results and finding the sort control, then answered from a
  results page.
- With the bound lifted, the two booking turns that reached a hotel and then
  its guest-details form took 18 and 16 requests. They cost 1.2 and 1.1 US
  cents as recorded, because most input was a cache hit.
- Allen, 2026-09-25, after those runs: raise the tool limit, because 5 is not
  enough.

## Decision

Take the Tier 2 loop's bound from `llm.max_tool_iterations`, 5 when unset.
Allen's config sets 40. ADR 0030's answer request after the bound still
follows.

## Alternatives rejected

- **A per-tool budget that only browser tools raise** (built first for ADR
  0059). It touched the tool definition, the MCP wrapper and the loop, and it
  kept every other turn at 5. Allen asked for the higher limit on every turn.
- **Keep 5 and let a booking span several turns** — each turn ends in an
  answer, so Allen would have to say "continue" three or four times per
  booking.

## Consequences

- A turn that never converges now spends up to 40 requests before its
  answer request. At the rates the booking turns recorded, that is about 3
  cents, and a minute or more before Jarvis answers. Allen's interruption
  still cancels it.
- A voice turn that loops holds the conversation for that long, since the
  answer is spoken only once it is complete.
- Each request carries about 32K input tokens, and cached ones count toward
  the model's per-minute limit: a 2026-09-26 replay at about 20 requests a
  minute drew some 50 rate-limit refusals. A long turn can hit that limit,
  and a refused request today ends the turn with no answer.
