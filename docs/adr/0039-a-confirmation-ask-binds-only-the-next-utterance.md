# ADR 0039 — A confirmation ask binds only the next utterance

**Status:** Accepted
**Date:** 2026-09-24
**Supersedes:** none

## Context

- ADR-0012 row C4 kept a pending ask alive across unrelated turns until its
  10-minute TTL (`confirmation.ttl_ms: 600000`). The only L3 tool then was
  `write_file`, and it is frozen.
- ADR 0033 sends every MCP tool that needs approval through the same path:
  GitHub merge/push/delete, Notion page writes, Microsoft To Do and calendar
  writes. Each one is irreversible outside Jarvis.
- The yes grammar is single words that Allen says to any question: 好, 好的,
  是, 可以, ok, yes (`config/confirm_grammar.yaml`). The grammar runs before
  Tier 0 and the model, so a match never reaches the model.
- Audit 2026-09-23 finding H1-01 reproduced it against a real MCP server:
  ask → 「现在几点」 → the model's answer ends with 「要我顺便把明天的日程也念一下吗？」
  → 「好」 executed the earlier ask, and the question Allen actually answered
  never reached the model.
- Allen approved on 2026-09-24: an ask stays valid only if the very next
  utterance answers it.

## Decision

A pending confirmation ask is answerable only by the next user utterance.
When that utterance does not match the grammar, append
`confirmation.rejected` with `grammar_rule_id: superseded_by_turn` for the
slot before Tier 0 runs, and handle the turn as one with no pending ask. The
TTL still applies to an ask nobody answers.

## Alternatives rejected

- **Keep C4 and shorten the TTL** — the H1-01 sequence (ask, unrelated
  question, yes) took under a minute in the reproduction; any TTL long
  enough to answer a spoken ask is long enough for that sequence.
- **Require a longer yes sentence (「执行 echo add」)** — every yes in the
  grammar would change, and it still fires when the model's own question
  happens to name the same tool.
- **Let the model decide whether a 「好」 answers the ask** — ADR-0012 D6
  keeps any model output from reaching an L3 dispatch; this reopens it.

## Consequences

- Asking a side question before answering loses the ask; Allen has to
  request the action again and answer the fresh ask.
- The closing turn tells nobody the ask was dropped; the model no longer
  sees a pending note on that turn, and only the event log records it.
- A paraphrased yes (「行吧那就写进去吧」) closes the ask as well. The model may
  propose the action again, which asks again.
