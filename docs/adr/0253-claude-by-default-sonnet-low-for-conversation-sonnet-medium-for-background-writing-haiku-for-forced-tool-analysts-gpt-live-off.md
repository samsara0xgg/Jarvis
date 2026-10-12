# ADR 0253 — Claude by default: Sonnet low for conversation, Sonnet medium for background writing, Haiku for forced-tool analysts, GPT-Live off

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- Allen approved moving every default model call from OpenAI to Claude. Sonnet 5.5 (replay
  exam 2026-10-11, ADR 0241) answers the conversation at roughly Haiku latency with the
  best pass rate; Haiku 5.5 is the cheap tier.
- Sonnet 5.5 takes adaptive thinking only: `thinking: disabled`, `budget_tokens` and so
  `reasoning_effort: none` are rejected. `request_fields` disabled thinking on every forced
  tool call, so a forced call on Sonnet failed outright.
- The daily report needs 32768 output tokens and about 240K input tokens, and forces its
  report tool in its last query round and on every retry. Work state, project sorting and
  mail summaries force a tool call on every request, many times a day.
- Setup and boot key checks assumed an OpenAI key and sent the default preset's model to
  the OpenAI SDK.
- GPT-Live is an OpenAI realtime websocket; no Claude model replaces it.

## Decision

The shipped defaults are Claude: conversation `sonnet` (effort low), the think turn
`sonnet-high` (max_tokens 32768, shared with the report), the daily report `sonnet-high` (quality over latency: it reads about 240K tokens), the vision reads and dictation polish `sonnet`, the quality background
writers (day summary, ledger prose, core memory, session
compaction) `sonnet-medium`, the frequent forced-tool analysts (work state, hence projects
and mail summary) `haiku-bg`, and `realtime.gpt_live.enabled` is false. For a model that
takes adaptive thinking only (`claude-sonnet-5*`), a forced tool call is sent as
`tool_choice: auto` with thinking kept, and each caller that needs the call checks for it;
Haiku still forces the call with thinking off. Setup and boot key checks follow the default
preset's provider.

## Alternatives rejected

- **Sonnet for the forced-tool analysts too** — it cannot be forced; every work-state run
  would depend on the model choosing to call, and Haiku at `haiku-bg` is several times
  cheaper per call on a path that runs many times a day (prices $0.10/$0.50 against $2/$10
  per million tokens).
- **Keep GPT-Live on with an OpenAI key beside the Anthropic one** — it keeps a second key
  and a second bill for one feature; the owner chose off.
- **Prefill or a stop sequence to force the call on Sonnet** — Sonnet 5.5 rejects assistant
  prefill, so only `auto` with a retry remains.

## Consequences

A forced call on Sonnet can come back with no tool call; the daily report retries (bounded
by `MAX_ROUNDS`) and work state or projects on a Sonnet preset would report a failed run
instead of a forced answer. The first-run desktop page still asks for an OpenAI key only.
Dictation latency now rides Sonnet at effort low, not a non-thinking OpenAI model. The
unused OpenAI presets stay in `config/jarvis.yaml`, and the code fallbacks for a missing
`work_state.preset` and `ledger.prose.preset` still name them.
