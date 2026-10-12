# ADR 0239 — The Dashboard shows Anthropic spend from the brain's own recorded calls

**Status:** Accepted
**Date:** 2026-10-12
**Supersedes:** none

## Context

- Yilun, 2026-10-12, via the coordinator: the brain sometimes calls Anthropic with
  its API key (ADR 0220), but the Usage page records only OpenAI's API spend. Claude
  Code runs on his Max subscription, so its logged cost is not API spend.
- OpenAI's row comes from its org Costs API with an admin key (ADR 0018). Anthropic's
  Cost API takes an admin key, or a personal key not scoped to a workspace, but its
  docs say the Admin API is unavailable to individual accounts, and his Console
  organization is an Individual Org with no Admin keys page. Whether his API key is
  accepted is unknown until a call is made.
- Every brain call already writes a `cost.recorded` row with provider, model and
  priced dollars (ADR 0173), and the Anthropic models are in `data/pricing.json`.
- The usage observer collects off the loop thread (`asyncio.to_thread`), and the
  event log connection belongs to the loop thread.

## Decision

The brain's usage observer emits an `anthropic` service whose snapshot is today's
and the month's `cost.recorded` dollars for provider `anthropic`, by model, read
from the log on the loop thread, in the same `usage.state_observed` shape as
OpenAI's, and, when Anthropic's own Cost API answers for the brain's key, that
answer as `official`, which the page then shows instead.

Its limits:

- The recorded figure counts only this brain's calls, priced at record time.
  `unpriced_calls` counts calls the provider reported usage for that had no price.
- The Cost API is asked with `ANTHROPIC_ADMIN_KEY`, else the brain's own
  `ANTHROPIC_API_KEY`, only in a request header. A refusal is kept as `official_error`
  (the HTTP status) and not retried for an hour; no key is ever stored or logged.
- The key reaches the brain the way OpenAI's does: pasted under Settings, Accounts, checked
  by listing one model, and kept in the brain's own store (the Keychain, or the `env` file
  on the Pi), so no key passes through a chat or a command line.
- No balance is shown.

## Alternatives rejected

- **Only the Cost API** — an individual org may be refused, and then the row would
  say nothing; the recorded calls are always there.
- **Convert the org to a team org for an admin key** — Anthropic documents no way
  back and no effect on credits or billing.
- **Read the log inside the collector thread** — the connection is bound to the
  loop thread and sqlite raises on use from another.

## Consequences

- Without `official` the figure is the brain's recorded spend, not the account's,
  and undercounts anything else billed to the key.
- Adding an Anthropic model without a price row shows as `unpriced_calls`, not a
  wrong dollar figure.
