# ADR 0002 — Mac-Only First Round

- **Status:** Accepted
- **Date:** 2026-05-17

## Context

The full spec ([`docs/spec.html`](../spec.html) §3.7) defines L6 as a federation
of Mac / RPi / Edge / Cloud. RPi owns Hue authority, MQTT broker, ESP32 sensor
ingestion, presence inference, ambient home mode, and home-side deferred
execution backup.

Building both Mac and RPi in parallel doubles the architecture surface area
before the spine (L2 / L3) has been proven in production. Mac is the daily
driver and the primary work substrate.

## Decision

First round is **Mac single domain**. The following are explicitly deferred:

- RPi domain (`deployment/domains/rpi.py`)
- `SyncBridge` / `ImportedEvent` / `cross_domain.*` event family
- MQTT broker / transport
- Hue / smart home tools (and the `ambient_act` attention channel)
- ESP32 sensor ingestion / `presence.inferred`
- Schema versioning *across* domains (within-domain versioning still applies —
  see [`ADR 0004`](0004-event-registry-versioning.md))

What stays even Mac-only: L6 itself does not disappear. Mac sleep/wake protocol,
local artifact store, worker-as-bounded-execution-context, and deferred
execution via macOS `launchd` are still real concerns and live in
`jarvis/deployment/`.

## Consequences

**Positive**
- L1–L5 designed once and stable; future RPi addition is purely additive
  (new `transport/` + new `domains/rpi.py`, plus a few new event families)
- Zero distributed-system mental load during M0–M5
- Mac sleep/wake correctness is exercised early and continuously

**Negative**
- No ambient room control; no presence-driven behaviour; no Hue
- `~/Projects/jarvis-legacy/` retains existing Hue stack as a fallback if Allen
  needs it before RPi expansion lands
- The DeferredExecution backup story is single-point — if Mac is asleep at
  fire time, `launchd` is the only line of defence (vs RPi backup in the full
  spec)

**Reversibility**
- Adding RPi later does not require restructuring L1–L5. The `deployment/`
  package has a reserved `transport/` slot and the `domains/` directory is
  ready for `rpi.py`.
