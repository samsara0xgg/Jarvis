# ADR 0166 — A spoken turn asks OpenAI for its Fast service tier

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- The wait between Allen's words and the first spoken word is dominated by the
  first token of the model's first request. On gpt-6-luna through the Responses
  API, 16 paid calls (2026-10-04) with `service_tier: "priority"` came back
  labelled `service_tier: "fast"` (OpenAI renamed Priority to Fast) and cut the
  first-token median from 1.04 s to 0.68 s and the maximum from 1.86 s to 0.85 s.
- Fast is billed at a premium. The 2x figure (gpt-6-luna input 0.20, cache read
  0.02, output 1.00 USD per 1M tokens) is second-hand and not checked against
  OpenAI's pricing page; the cache-write rate is an assumption.
- Only a spoken turn is waited on. Daily reports, mail and job-mail jobs, the
  day summary, core memory, compaction, projects, vision, dictation, the
  prefix warm and every other background request have no listener, and a typed
  turn has a screen to wait on.
- The field is OpenAI's own. Sent to OpenRouter (Jev), x.ai or Anthropic it is
  at best ignored and at worst a rejected request.
- `cost.recorded` prices a request from its model row. The tier the provider
  says it served is what it bills, and can differ from the tier asked for.

## Decision

Send `realtime.response.voice_service_tier` as the `service_tier` of every
model request `decide()` makes for a turn Allen spoke.

Its limits:

- **Off by default.** An empty or absent key sends nothing, and the request body
  is then unchanged.
- **Voice turns only.** The composition root sets it on the turn's
  `DecideContext` only for a spoken channel (not typed, not GPT-Live). It covers
  the spoken stream and its tool-loop follow-ups, the routine stream, the batch
  loop, the answer after the tool budget and the spoken-form rewrite, which
  belongs to the voice answer. Nothing else reads the key.
- **OpenAI's host only.** The client drops the field unless the provider is
  OpenAI and the base URL is api.openai.com, whatever the caller passes.
- **Per request.** The tier is an argument of one request, captured into its body
  at prepare time; it is never stored on the client or a preset.
- **Billed as served.** The cost recorder prices a request at the tier the
  response reports, else the tier asked for, from a `<model>:<tier>` row in
  `data/pricing.json` (`priority` is `fast`); a tier with no row bills at the
  model's standard row. `cost.recorded` carries the tier as the optional
  `service_tier`.

## Alternatives rejected

- **Set the tier on the preset.** The preset serves every kind on that model, so
  daily reports and mail jobs would pay the premium for latency nobody waits for.
- **Send it for every request of a model.** Same cost, and it cannot be kept off
  OpenRouter or x.ai by configuration alone.
- **Keep the standard price for fast requests.** `cost.recorded` would run at
  half the real rate on exactly the turns the switch changes, and the cost guard
  and every report built on it would under-count.

## Consequences

- A turn on Fast costs about twice as much per token until the price row is
  checked against OpenAI's page; the row's `source` says it is unverified.
- A model with no `<model>:fast` row is billed at its standard rate on Fast, an
  undercount, until a row is added.
- Fast is an OpenAI capacity tier: whether it keeps its latency under load, and
  its rate limits, are not measured here.
