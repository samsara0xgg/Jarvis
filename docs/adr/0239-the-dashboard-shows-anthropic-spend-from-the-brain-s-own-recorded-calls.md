# ADR 0239 — The Dashboard shows Anthropic spend from the brain's own recorded calls

**Status:** Accepted
**Date:** 2026-10-12
**Supersedes:** none

## Context

- Yilun, 2026-10-12, via the coordinator and in the thread: he wants all his Anthropic
  API spend, tests that never passed through the brain included, and the credit left
  on his account. Claude Code runs on his Max subscription, which is not API billing.
- OpenAI's row comes from its org Costs API with an admin key, and its balance is typed
  once and reduced by the spend since (ADR 0065). Anthropic reports no balance either.
- Anthropic's Cost API takes an admin key, or a personal key not scoped to a workspace,
  and its docs say the Admin API is unavailable to individual accounts. His Console
  organization is an Individual Org with no Admin keys page, and its API keys can be
  created at Organization scope. Whether such a key is accepted is known only by a call.
- Every brain call already writes a `cost.recorded` row with provider, model and priced
  dollars (ADR 0173), and the Anthropic models are in `data/pricing.json`.
- The usage observer collects off the loop thread, and the event log connection belongs
  to the loop thread.

## Decision

The brain's usage observer emits an `anthropic` service in the same `usage.state_observed`
shape as OpenAI's: the whole account's cost today and this month by model, read from
Anthropic's Cost API with `ANTHROPIC_ADMIN_KEY`, else the brain's own `ANTHROPIC_API_KEY`,
as `official`; and the `cost.recorded` dollars for provider `anthropic`, read from the log
on the loop thread, as the fallback the page shows while `official` is absent. The balance
Allen types from the Console Billing page is kept as for OpenAI, and shown as that figure
minus `official` cost since its UTC day.

Its limits:

- A refusal is kept as `official_error` (the HTTP status) and not retried for an hour; no
  key is stored or logged, and the key goes only in a request header.
- An organization-scope key can read the report but cannot call a model without a workspace
  header, so Claude calls keep their own key (`ANTHROPIC_API_KEY`, a Default-workspace key)
  and the report is read with a second one (`ANTHROPIC_ADMIN_KEY`).
- Both keys reach the brain as OpenAI's does: pasted under Settings, Accounts, checked by
  listing one model, and kept in the brain's own store (the Keychain, or the `env` file on
  the Pi).
- With no `official` there is no balance, only the recorded figure and its date.
- The recorded figure counts only calls the brain recorded; `unpriced_calls` counts those
  whose model had no price.

## Alternatives rejected

- **Only the brain's recorded calls** — his tests and any other API use never pass through
  the brain, so it cannot answer the credit left on the account.
- **Convert the org to a team org for an admin key** — Anthropic documents no way back and
  no effect on credits or billing.
- **Read the log inside the collector thread** — the connection is bound to the loop thread
  and sqlite raises on use from another.

## Consequences

- If Anthropic refuses the key, the page falls back to the brain's own spend and shows no
  balance until a key it accepts is saved.
- The balance errs low by up to a day of spend, and by promotional credit that expires.
