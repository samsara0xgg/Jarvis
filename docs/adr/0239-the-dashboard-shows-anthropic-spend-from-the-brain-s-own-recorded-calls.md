# ADR 0239 — The Dashboard shows Anthropic spend from the brain's own recorded calls

**Status:** Accepted
**Date:** 2026-10-12
**Supersedes:** none

## Context

- Yilun, 2026-10-12, via the coordinator: the brain now calls Anthropic directly
  (ADR 0220), but the Usage page records only OpenAI's API spend.
- OpenAI's row comes from its org Costs API with an admin key (ADR 0018).
  Anthropic's equivalent, the Cost API, needs an organization admin key, a second
  secret the brain does not hold and a personal account may not have.
- Every brain call already writes a `cost.recorded` row with provider, model and
  priced dollars (ADR 0173), and the Anthropic models are in `data/pricing.json`.
- The usage observer collects off the loop thread (`asyncio.to_thread`), and the
  event log connection belongs to the loop thread.

## Decision

The brain's usage observer emits an `anthropic` service whose snapshot is today's
and the month's `cost.recorded` dollars for provider `anthropic`, by model, read
from the log on the loop thread, in the same `usage.state_observed` shape as OpenAI's.

Its limits:

- It counts only this brain's calls, priced at record time; Claude Code sessions,
  the subscription and anything else on the account are not in it.
- `unpriced_calls` counts the month's calls the provider reported usage for and
  that had no price, so a model missing from the price table shows on the page.
- No balance is shown, and no key is read.

## Alternatives rejected

- **The Anthropic Cost API with `ANTHROPIC_ADMIN_KEY`** — needs a second key kept
  on the brain and an organization account; the log already has every brain call.
- **Read the log inside the collector thread** — the connection is bound to the
  loop thread and sqlite raises on use from another.

## Consequences

- The figure is the brain's spend, not the account's, so it undercounts anything
  billed to the same key elsewhere.
- Adding an Anthropic model without a price row shows as `unpriced_calls`, not a
  wrong dollar figure.
