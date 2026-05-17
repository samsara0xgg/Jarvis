# ADR 0001 — State-Centric Runtime

- **Status:** Accepted
- **Date:** 2026-05-17

## Context

Industry default for a personal assistant is "LLM + chat history + tool calls".
This works for one-off interactions but fails the core requirement: Allen
should not have to re-explain context across sessions, surfaces, or restarts.
Chat history is ephemeral, gets dumped wholesale into prompts, and rots —
the LLM's "memory" is whatever fits in this turn's context window.

We need a substrate whose identity survives surface changes (voice → web →
notification), LLM provider changes, prompt rewrites, and tool churn.

## Decision

Jarvis identity lives in a durable **State Object** = **Event Log
(append-only, immutable) + Projections (read-only fold)**. Everything else —
LLM, prompts, tools, voice, UI — is replaceable surface on top of that spine.

- All persistent change goes through canonical events
- Projections are derived (folded from events), never directly mutated
- LLM is a stateless calculator that reads a *Situation Packet* and returns
  proposals; it cannot mutate state
- Cross-session continuity is automatic (replay-from-log semantics)
- The LLM does not "remember" anything; the State Object does

See [`docs/spec.html`](../spec.html) §2 and §3 for the full architecture.

## Consequences

**Positive**
- Surfaces become replaceable — Voice / Inherent / notification / Mac observer
  all read projections and emit events; switching providers does not lose state
- Audit trail is complete by construction; replay is deterministic
- Concurrency is safe: append-only events + supersede-by-append for corrections
- Cross-domain federation (future RPi) reuses the same protocol

**Negative**
- Event schema becomes a primary engineering surface (locked in
  [`ADR 0004`](0004-event-registry-versioning.md))
- Every turn pays the cost of loading a projection snapshot
- Naive "just dump chat history into prompt" patterns are forbidden — must go
  through `Situation Packet` assembly

**Forbidden** (architectural violations)
- LLM directly marking `task.verified_complete`
- Surface (UI) caching active_task as truth in localStorage
- Tool registry mutating projection rows
- Prompt-history-as-state of any kind
