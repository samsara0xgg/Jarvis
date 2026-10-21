# ADR 0339 — The daemon's code lives in a package, one module per concern

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- `jarvis/runtime/inherent_loop.py` held the daemon's entry point, `serve_inherent`, and, beside
  it, every concern the daemon had grown: the watchers that carry events out to the surfaces, the
  supervisor and data sweeps, the audio-device and power watches, the voice builders, the Live
  backend and the route dependencies. It reached 8,312 lines and 121 top-level definitions, and
  most of those groups read nothing from one another.
- A test that drives daemon behaviour patches the module attribute the code reads, so where a
  function lives is part of its test seam. Several canaries also pin a file path and read its
  source.
- The six layers are enforced by `lint-imports`, and `runtime/` is the one place allowed to wire
  across them. A split inside `runtime/` changes no layer contract.
- The lead-in commentary path and `serve_inherent` each have an owner who is still changing them.
  The voice code that speaks a conversation line reads the commentary path's helpers (its
  terminalizer, its pre-emit verdict, the answer-started query and two time windows), so neither
  side can move alone.

## Decision

Daemon code lives in `jarvis/runtime/daemon/`, one module per concern, and new daemon code goes
in the module that owns its concern, not in `inherent_loop.py`.

- `inherent_loop.py` keeps `serve_inherent` and the commentary path until their owners split
  them; code that reads the commentary path's helpers stays beside it.
- A name another module imports is public. No module re-exports another's names; `serve_inherent`
  may read a moved name under its old spelling through an `import ... as` alias until it is
  split.
- A move changes no behaviour: the code is moved verbatim, and the tests that exercise it patch
  the module that now reads the name.

## Alternatives rejected

- **Top-level modules in `jarvis/runtime/`** — the directory already holds 62 files, and the
  daemon's modules read one another's public names; a package shows which of them are the
  daemon's and keeps them reviewable as one unit.
- **One file with section markers** — a marker gives a group no seam to patch and no path for a
  canary to pin: three canaries named this file's path to check one watcher or sweep function
  each, and a change anywhere in the 8,312 lines was a change to the file they pin.
- **Moving `serve_inherent` and the commentary path in the same change** — it rewrites lines two
  owners are editing, so each edit would conflict with the move, and the commentary helpers have
  no module to move to until their owner splits them.

## Consequences

- Until the commentary path is split, the voice code that speaks a conversation line (the
  conversation line itself, the cancel-voice-runs hook, the local listen ports, the single-ingress
  session and the phone hub) and the job, plan and reminder lines stay in `inherent_loop.py`,
  which keeps the file larger than the concern list suggests.
- A test that patches a moved name on `inherent_loop` patches only the alias `serve_inherent`
  reads; the moved code reads its own module's attribute, so the patch is silent. The test has to
  patch the new module.
- Log lines from moved code carry the new module's logger name, so a log filter on
  `jarvis.runtime.inherent_loop` no longer sees them.
