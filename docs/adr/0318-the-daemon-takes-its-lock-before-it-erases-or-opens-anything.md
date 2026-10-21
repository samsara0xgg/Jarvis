# ADR 0318 — The daemon takes its lock before it erases or opens anything

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

`jarvis serve` ran `bootstrap_runtime_app` first (the pending erase of ADR 0067, the event log, the
`memory.db` migrations, a geocoding thread) and took `daemon.lock` only afterwards, inside
`serve_inherent`. A second `serve` on a held root therefore wrote to it before it learned the lock
was taken.

The erase was carried out by `bootstrap_runtime`, which every entry point calls: `jarvis "…"`, the
imports, the voiceprint and MCP-login commands. `POST /inherent/data/erase` leaves the marker down
while the daemon restarts, so any of them booting in that window emptied the root under a running
daemon. The erase also deleted `daemon.lock` itself: reproduced with a marker and a held lock, the
second `serve` made a new lock inode, took it, and ran beside the first.

Constraints: the lock file lives in the root, so the root exists first; ADR 0067 wants the erase
before anything under the root is open; `flock` is held by the process, so whoever holds it must
outlive everything that opens the root.

## Decision

`jarvis.deployment.daemon_session` takes the lock, then carries out a pending erase (keeping
`daemon.lock`), then lays out the paths; `jarvis serve` enters it before it bootstraps and leaves it
after the Event Log is closed, and `serve_inherent` runs inside it without taking the lock itself.
No other entry point erases: the marker waits for the next daemon.

## Alternatives rejected

- **Probe `is_held` before erasing in `bootstrap_runtime`** — a probe is not a hold. A daemon that
  takes the lock between the probe and the `rmtree` is emptied all the same, the window this closes;
  and the erase would still run from commands that are not the daemon.
- **Only keep `daemon.lock` out of the erase** — this ends the second lock inode, but a second
  `serve` would still migrate and write before it is refused, and a one-shot command would still
  empty a root whose daemon is mid-restart.
- **Move the erase into `serve_inherent`** — it runs after the event log and `memory.db` are open,
  which is what ADR 0067 rules out.

## Consequences

A pending erase is carried out by the next `jarvis serve` only; a one-shot command, an import or a
login leaves the marker and the data alone. The lock is held through the whole bootstrap, so a
second `serve` during the first one's start is refused instead of racing it. Nothing in the types
stops a caller from running `serve_inherent` without the session; `jarvis serve` is the only one.
