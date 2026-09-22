# ADR 0022 — Resonance Codex activity retention

**Status:** Accepted
**Date:** 2026-09-20
**Supersedes:** none

## Context

Allen requested a compact, six-row Codex activity surface with expanding
capsules, a rolling day of recent sessions, long-press pinning and local
archive. The existing newest-eight observer can evict a still-running turn.
Desktop Codex sessions remain observation-only under ADR 0019; their running
turns cannot be controlled by Jarvis's separate worker app-server.

## Decision

Keep recent-session presentation preferences and pinned snapshots in Resonance,
retain active turns independently of list capacity, and treat archive as a
local dismissal of one turn rather than deletion or control of a Codex thread.

## Alternatives rejected

- **Keep only the newest eight sessions** — a ninth session removes an active
  task even though it still needs monitoring.
- **Use worker controls for desktop sessions** — those sessions belong to a
  different app-server and cannot be interrupted through Jarvis's worker client.
- **Delete Codex threads on archive** — local list cleanup would then destroy
  or reorganize history in the source client.

## Consequences

Pinned snapshots can outlive the observer and must not be presented as live
state after observation is lost. Reply drafts can be composed inline but direct
send and stop remain unavailable until a supported desktop control path exists.
