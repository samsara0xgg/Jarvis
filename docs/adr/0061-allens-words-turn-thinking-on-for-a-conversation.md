# ADR 0061 — Allen's words turn thinking on for a conversation

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- Conversation runs on gpt-5.6-luna with `reasoning_effort: none`. A
  2026-09-26 replay of 111 of Allen's past utterances found that low thinking
  moved the median no-tool answer from 1.9 s to 3.1 s. Most of that is
  /v1/responses, which this model needs to think and use tools at once. So
  the default stays none.
- Allen, 2026-09-26: keep the default without thinking, but let him make it
  think by voice: "想想吧" and similar words switch it on, and then he keeps
  talking to it.
- Allen asked for his own words, not the model's judgment, to decide. Asking
  the model first would add about 0.9 s to every sentence.

## Decision

- `llm.think` names a preset and two patterns, `on_words` and `off_words`
  (not `on` and `off`, which YAML reads as booleans). Before a turn
  opens its ResponseRun, the runtime reads Allen's recent words
  (`surface.user_intent`, `utterance.received`) back from the event log,
  newest first. An `on_words` match that is newer than any `off_words`
  match, inside an unbroken conversation, gives the turn `llm.think.preset`. A conversation
  breaks at a gap of more than 10 minutes. Anything else gives the default
  preset.
- The sentence holding the on-word already thinks.
- `response.started` carries the preset's `reasoning_effort`, so each answer
  records whether it thought.
- Allen's config uses `luna-think`: the same model, on /v1/responses, with
  medium effort.

## Alternatives rejected

- **A stored mode flag.** It would need its own event, and a way to recover
  after a restart. Reading his words back needs neither.
- **One-sentence triggers only.** Allen asked to switch a mode and then keep
  talking.
- **A model call that judges each sentence's difficulty.** It would be slow
  on every turn, and Allen asked for his own words to decide.

## Consequences

- "我想一想" said to himself also turns the mode on. An off-word or ten
  quiet minutes end it.
- A thinking turn's spoken-form rewrite thinks too, because it uses the same
  request client.
- Only a turn that opens a ResponseRun (`realtime.response.response_run_lifecycle`)
  can switch. Without one, every turn uses the default preset.
- The companion does not show the mode yet.
- Live on a side daemon, 2026-09-26: "想想吧，我这周应该先推进哪个项目？"
  thought (medium, 225 reasoning tokens, 7.7 s), and so did the next
  sentence (79 tokens, 4.4 s). "不用想了，…" and a greeting after it did not.
