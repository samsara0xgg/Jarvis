# ADR 0004 — Event Registry & Schema Versioning

- **Status:** Accepted
- **Date:** 2026-05-17

## Context

Events are append-only and immutable ([`ADR 0001`](0001-state-centric-runtime.md)).
The system is expected to run for years. Schemas will need to evolve:
new event types, new payload fields, refined semantics. Naive evolution —
mutating Pydantic models in place, deleting old types, dropping fields —
silently breaks replay and creates undebuggable production drift.

The spec ([`docs/spec.html`](../spec.html) §5.4 + §3.7.6) requires:
- Event type and payload schema must be versioned
- Cross-domain unknown event must quarantine, not crash
- Replay must remain deterministic for any historical event

## Decision

**Single source of truth, centralised dispatch, additive evolution.**

1. **Schemas live in one place only.** All event payload Pydantic models live
   under `jarvis/state/events/types/<family>.py`, one file per event family
   (`action`, `worker`, `claim`, `task`, `surface`, …). No other layer may
   define event payload schemas.

2. **Central registry.** `jarvis/state/events/registry.py` imports every type
   module and builds a `{event_type → PydanticModel}` dispatch table at module
   import time. `emit_event(type, payload)` looks up the model, validates, and
   wraps in an envelope.

3. **Every event carries `schema_version: int`** in its envelope
   (`jarvis/state/events/envelope.py`). The envelope also carries `event_uid`
   (UUIDv7), `origin_domain`, `ts`.

4. **Evolution is additive.** Adding fields with safe defaults bumps the
   minor version implicitly (decoder tolerates absence). Removing or changing
   field semantics requires a new event type (`task.verified` →
   `task.verified_v2`) and a `correction.*` / `supersede.*` migration of
   downstream projections. Old payload types remain valid decoders.

5. **Unknown / unsupported events quarantine, never crash.** A reader that
   does not recognise an event type or whose decoder version is older than the
   payload emits `cross_domain.event_quarantined` (or local equivalent) and
   skips during projection fold. Upgraded readers may later replay the
   quarantine queue.

6. **Migrations are explicit.** Schema changes that affect storage shape
   require a numbered migration under `jarvis/state/store/migrations/`. The
   migration runner is a `system_maintenance` caller_principal tool.

## Consequences

**Positive**
- Old events always decodable; replay survives all future schema work
- New events may carry payloads older readers cannot interpret — quarantine
  prevents poisoning the projection without losing the event
- Adding an event type is a 2-line operation (define model + register) — there
  is no incentive to scatter event definitions across other layers
- Cross-domain (future RPi) inherits the same discipline by default

**Negative**
- Cannot "just rename a field" — must add new event type and supersede
- `registry.py` becomes the import chokepoint; circular import risk if event
  types reference other layers (they must not)
- Migration discipline must be maintained even for "small" schema changes

**Enforced by**
- `import-linter` contract: only `jarvis.state.events.*` may define types
  inheriting from `EventPayload` (or equivalent base)
- `tests/contract/test_event_payload_bounded.py`: every registered event has
  a size-bounded payload
- Code review checklist for any commit touching `state/events/`
