# ADR 0220 — Claude Haiku 5.5 is a first-class preset on every request path

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- Allen wants to be able to switch the brain's default model from `gpt-6-luna` to
  `claude-haiku-5-5`, called on Anthropic's API directly (never through a router), for cost and
  answer quality. The default stays `luna` until a replay eval of real logged requests decides.
- Before this change an `anthropic` preset sent only `max_tokens`, `system`, `messages` and
  `tools`. It dropped the spoken-reply schema (ADR 0114), set no thinking and no caching, kept
  OpenAI-shaped tool history, was gated out of the spoken route (`provider == "openai"`) and
  out of prefix warming, opened a new connection per request, and reported `input_tokens`
  without its cached part, which `compute_cost_usd` subtracts as if it were included.
- Haiku 5.5 takes `thinking: adaptive` with `output_config.effort`, structured output through
  `output_config.format`, automatic caching through a top-level `cache_control`, and `max_tokens: 0`
  to fill the cache. It rejects assistant prefill, non-default sampling parameters, extra keys on
  tool definitions, and any forced tool choice combined with thinking. Its cache is keyed on
  tools, system, messages and the structured format, and a changed thinking setting breaks it.
- Measured on real logged decision requests, warm, first token median: Haiku adaptive thinking at
  effort low 0.62 s; Haiku with thinking disabled 1.4 s (and it made spurious tool calls);
  gpt-6-luna at the priority tier live voice turns use (`realtime.response.voice_service_tier`)
  0.85 s on the Pi and 0.75 s on the Mac, a full turn about 1.2 s for both models. Speed is a tie;
  the case for Haiku is cost and answer quality, not latency.
- The history every caller keeps is one shape (chat/completions: `role: tool`, assistant
  `tool_calls`, `image_url` parts, a `phase` label), and background jobs build their own
  one-preset clients, so the adaptation has to sit inside `LLMClient`, not in each caller.
- Haiku 5.5 has no priority tier and no fast mode: `service_tier` and the OpenAI-only
  `extra_body` fields (`prompt_cache_key`, flex) mean nothing to it.

## Decision

An Anthropic preset gets the same behaviour as an OpenAI one on every `LLMClient` path
(`stream_events`, `chat`, `chat_stream`, prefix warming), with adaptive thinking at the preset's
effort rather than disabled thinking, and `luna` stays the default.

- `reasoning_effort` is the one preset knob: `none` disables thinking, `minimal` means `low`, any
  other effort is adaptive thinking at that effort; a forced tool call disables thinking.
- Every Anthropic stream request and every tool-bearing `chat` carries top-level `cache_control`;
  one-shot calls without tools do not, so they never pay a cache write nothing reads.
- Thinking blocks of a tool-using response are kept by tool call id and returned unchanged in the
  assistant turn that precedes its results, so history keeps its single shape.
- Prefix warming on Anthropic sends the next turn's tools, system, thinking and format with
  `max_tokens: 0`, ending on the last user message.
- `service_tier`, `extra_body` and the Responses `phase` labels are ignored on Anthropic.

## Alternatives rejected

- **Thinking disabled for the voice preset** — first token median 1.4 s against 0.62 s with
  adaptive thinking at effort low, on the same logged requests, and it made
  spurious tool calls.
- **Anthropic-shaped history in the decision loop** — would fork every caller (decision loop,
  daily report, work state) for a provider they are meant not to know; the conversion in
  `LLMClient` is 100 lines and one test table.
- **Caching every request** — a one-shot call (summaries, dictation, vision) pays 1.25x on its
  input for a cache that is never read; the tool loops and the spoken stream are where the prefix
  comes back.
- **Switching the default now** — latency is a tie at the priority tier and quality is unmeasured
  on this adaptation; a replay eval (`scripts/replay_decision.py`) comes first.

## Consequences

- An Anthropic stream opens one connection pool per key on the resident I/O loop, like OpenAI's.
- `data/pricing.json` carries two Haiku rows (`claude-haiku-5-5`, `claude-haiku-5-5:long`);
  `compute_cost_usd` switches to the `:long` card above 100K prompt tokens, cache included.
  The rows are hand-written in `scripts/refresh_pricing.py` and must be re-checked when
  Anthropic changes its card.
- The text before a tool call is not labelled on Anthropic, so a structured spoken turn relies on
  the model putting nothing before a call (the structured reply note already says so); if it does,
  that text is spoken as an answer.
- The thinking blocks are held in memory (512 tool calls); a restart between a call and its result
  loses them and the request goes without them.
- Background presets stay on OpenAI; `haiku-bg` is there to point them at when wanted.
