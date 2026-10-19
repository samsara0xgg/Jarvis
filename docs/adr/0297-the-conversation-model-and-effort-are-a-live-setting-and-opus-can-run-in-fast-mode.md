# ADR 0297 — The conversation model and effort are a live setting, and Opus can run in fast mode

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

- ADR 0253 fixed the conversation on `llm.default_preset` (Sonnet 5.5, effort low); ADR 0108's
  think turn uses `llm.think.preset`. Changing either was a YAML edit and a brain restart. He wants
  to pick the model and its effort on the desktop Settings page and on the phone, and hear the
  difference on the next sentence.
- Measured on the Pi on 2026-10-19, warm connection: Sonnet 5.5 at low reached the first token in
  0.82 s and finished the turn in 1.74 s; Opus 5.5 in fast mode in 1.20 s and 1.60 s. Opus
  answers better; fast mode is what makes it fit a spoken turn. Fast mode is a top-level request
  field `speed: "fast"` plus the header `anthropic-beta: fast-mode-2026-02-01`, at $8/$40 per
  MTok against $4/$20 (cache read $0.40, 5-minute cache write $10). Its cache is separate from
  standard speed, and it has its own rate limit.
- Opus 5.5 rejects `thinking: disabled` and a forced `tool_choice`, like Sonnet 5.5.
- The brain owns `llm` (ADR 0227). The desktop page talks to the Mac's terminal, which sends a key
  it does not own to the brain; the phone talks to the brain. Settings are read once at boot
  (ADR 0052) except the few that a running chain takes at once (ADR 0054, 0147).
- A request client is minted per ResponseRun from a frozen snapshot of one preset (ADR 0008 D4),
  and a snapshot hash is part of the run's identity.
- The Anthropic SDK's `messages.create` has no `speed` parameter.

## Decision

The conversation's model and effort are two live brain settings, `conversation_model` and
`conversation_effort`, and `opus-fast` is a preset that asks for fast mode.

- **Choices.** `opus-fast`, `opus`, `sonnet`, `haiku`, `luna`: each the llm preset of that name.
  Claude models take `low`, `medium`, `high`; luna also takes `none`. The think turn uses the
  chosen model at `high` (luna: on the Responses API, as `luna-think`); every other preset
  (background work, reports, dictation, vision) stays as configured.
- **Live.** A saved choice is applied by `LLMSessionFactory.choose` from the next snapshot, never
  leaves `restart_pending` true, and is kept in `settings.json` so a restart boots on it. Nothing
  saved: the conversation is `default_preset` exactly as before, with unchanged snapshot hashes.
- **Fast mode** is the preset field `speed: fast`, carried by the snapshot (in its hash only when
  set), sent as `speed` in the body (through `extra_body`, the SDK having no parameter) with the
  beta header merged into any other `anthropic-beta`. A fast request refused with a 429 before its
  first chunk is sent once more at standard speed. A response whose `usage.speed` is `fast` bills
  on the `<model>:fast` pricing row, as an OpenAI request served on the fast tier does.

## Alternatives rejected

- **Edit `default_preset` in the YAML and restart.** The phone has no file to edit, and a restart
  stops the brain for the time it takes to come back.
- **Switch the shared `LLMClient` with `switch_model`.** ADR 0008 D4 made request clients immutable
  because overlapping runs stamped each other's request ids; the factory's snapshot is the seam.
- **A setting per background preset.** Only the conversation is waited on, and the 2026-10-19
  numbers are about that path.
- **Fail a refused fast request, or wait and retry it in fast mode.** The first leaves a spoken turn
  without an answer; the second leaves it silent for the wait. Standard speed has no fast-mode limit.
- **Fast mode for every Opus use.** It costs twice the standard price and has a cache of its own,
  so plain `opus` stays a choice.

## Consequences

- A fast request that falls back to standard speed pays a cold cache write once, because the two
  caches are separate.
- Switching model mid-conversation also changes the prompt prefix the cache matches, so the first
  turn after a switch is a cache miss.
- Haiku keeps its preset's own effort (`medium`, ADR 0220) until an effort is saved; a saved
  effort stays across model picks and is replaced by the model's own when the new model does not
  take it.
- The non-streaming calls (`LLMClient.chat`) send the fast field and header too, but only the
  streaming path retries a 429.
- `data/pricing.json` carries the `claude-opus-5-5:fast` row by hand (`TIER_ROWS` in
  `scripts/refresh_pricing.py` keeps it); its prices came from the owner, not the pricing page.
