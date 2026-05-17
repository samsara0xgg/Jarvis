# ADR 0003 — Scenario-First MVP with Hard "Full 6-Layer Walk" Rule

- **Status:** Accepted
- **Date:** 2026-05-17

## Context

Two extreme implementation strategies are available:

1. **Spine-first:** build L2 (Event Log + Projections) in isolation, then L3,
   then L4, then L5 — no user-visible value for weeks, high risk of
   over-engineered infrastructure for hypothetical needs.
2. **Scenario-MVP (vanilla):** pick one feature, build it end-to-end with the
   smallest possible everything — fast demo, but invariably takes shortcuts
   ("LLM calls tool directly, we'll add the event log later") that become
   architecture debt.

Because the spec ([`docs/spec.html`](../spec.html)) is already detailed —
event registry, ToolDefinition, Situation Packet, gates — there is little
*discovery* value in pure spine-first. The risk is the opposite: building
abstractions without traffic to justify them.

## Decision

**Scenario-first**, with one inviolable rule:

> Every line of MVP code must walk through the full 6-layer architecture.
> No shortcuts even when shortcuts would be simpler.

The first scenario:

> Allen says: "Have Codex fix the dark-mode bug, ping me when done."
> Jarvis spawns Codex → worker runs in background → worker reports complete →
> Jarvis runs the tests independently → notifies Allen with honest framing
> ("Codex reported complete, tests pass, verified" vs "Codex reported complete
> but tests failed, not verified").

This scenario was chosen because it simultaneously exercises:

- **C3** (reduce context load — Allen does not babysit the worker)
- **C4** (supervisor not worker — Jarvis schedules, Codex executes)
- **C5** (evidence-bound — `reported ≠ verified` enforced in the output)
- All 6 layers (full L2 event lifecycle, L3 Pre-emit Gate, L4 worker +
  verification tool with `result_semantics`, L5 voice + notification, L6
  Mac-local artifact store + sleep/wake)

## Consequences

**Positive**
- The "scaffold first scenario" forces ~12 core event types, 3 projections,
  6 caller principals, `Pre-emit Gate v0`, and `ActionLifecycle` 8-state into
  existence simultaneously — the spine emerges as a byproduct of a real flow
- Subsequent scenarios (continue-yesterday, what's-Codex-doing, deferred
  reminder, attention modes, memory promotion) add tools / projection fields /
  channels but do *not* add new architecture
- Pre-emit Gate v0 (template + keyword guard) catches the most dangerous claim
  inflation from day 1

**Negative**
- Estimated 2-3k lines before the first end-to-end demo
- No user-visible features until M3 (first scenario completes)
- Requires discipline to refuse "just hack it" temptations during scaffolding

**Sequence after scenario 1**
- S2 "Continue yesterday" → Memory Projection, Drift Watch
- S3 "What's Codex doing" → Inherent visual cockpit (L5 second channel),
  Freshness UX
- S4 "Remind me tomorrow" → Scheduler + DeferredExecution
- S5 Mode switching (focus/rest) → Attention Policy channel scoring
- S6 "Remember I prefer X" → Memory promotion (4-question filter)
