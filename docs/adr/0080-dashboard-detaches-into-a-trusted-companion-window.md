# ADR 0080 — Dashboard detaches into a trusted Companion window

**Status:** Accepted
**Date:** 2026-09-27
**Supersedes:** none

## Context

Allen selected the second design's attached dusk Dashboard and asked to defer
window homes. Detaching a Dashboard must leave the character at the notch,
preserve live conversation and controls, and allow native movement between
displays. The existing Companion window is anchored to the menu bar and
passes pointer input through its transparent space. Its daemon bridge trusts
one local main frame.

## Decision

Present the detached Dashboard in one lazily created, trusted local Companion
window that reuses the same Dashboard composition and daemon services, while
main owns its bounds, docking and profile persistence; keep the character and
all home rendering in the notch window.

## Alternatives rejected

- **Move the existing Companion window** — this moves the character away from
  the hardware notch with it and cannot leave the island in place.
- **Move a panel inside a fullscreen overlay** — this cannot behave as an
  independent native window across displays and other application windows.
- **Copy the Dashboard implementation** — changes to live approval, plugin
  and conversation behavior would require matching edits in two components.

## Consequences

The bridge must explicitly recognize both local main frames without trusting
arbitrary windows. Shared profile preferences must propagate between them,
and persisted bounds must be clamped when a display is removed. Each visible
presentation reads current daemon state; moving between them does not migrate
or duplicate authoritative runtime state. A bounded, main-frame-only message
relay transfers the current page and unsent edits in memory, routes settings and
agent answers to the visible surface, and shares the character's emitted colour.
It does not persist drafts or replay pending actions and armed confirmations.
Window homes remain out of scope.
