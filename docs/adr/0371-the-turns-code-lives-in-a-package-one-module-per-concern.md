# ADR 0371 — The turn's code lives in a package, one module per concern

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- `jarvis/runtime/__init__.py` held the composition root's bootstrap and, after it, the turn: the
  cancel seams that stop or replace a response run, the wait for an action's trigger and the
  checkpoint a suspended turn leaves, the opening of a turn's run, the helpers that write and read
  back around an answer, the next prefix's warm-up, and `drive_turn`. The turn was about 2,000 of its
  4,606 lines, and the file took 50 commits in the week to 2026-10-20.
- Bootstrap and the turn share two types, `JarvisRuntime` and `RunTurnResult`. The turn's helpers
  read nothing from bootstrap's builders, and bootstrap calls none of the turn's.
- A test that drives the turn patches the module attribute the code reads, so where a function
  lives is part of its test seam. Four canaries read the source of the waiter, the trigger tuple,
  `drive_turn`'s release in a `finally` and the hot-path module list by file path.
- `drive_turn` is the hot path of the ADR 0008 response lifecycle, about 700 lines with every
  response-run branch flag-guarded. Its resume path and its first pass differ in what they carry,
  and a later change owns that.
- The six layers are enforced by `lint-imports`, and `runtime/` is the one place allowed to wire
  across them. A split inside `runtime/` changes no layer contract. ADR 0339 did the same for the
  daemon.

## Decision

The turn's code lives in `jarvis/runtime/turn/`, one module per concern, and
`jarvis/runtime/__init__.py` holds only the public types the turn and bootstrap share.

- Nothing in `jarvis/runtime/__init__.py` imports `turn/`; the turn's modules import the types from
  the package, so there is no cycle. What both need is a type there; what only the turn needs goes
  in the module that owns its concern.
- A name another module imports is public, in `turn/` and out of it. No module re-exports
  another's names, and every caller imports from the module that defines the name.
- A move changes no behaviour: the code is moved verbatim, and `drive_turn` moves whole. The tests
  that exercise it patch the module that now reads the name.

## Alternatives rejected

- **Split bootstrap out and leave the turn in `__init__.py`** — the file that remains would still
  hold 2,000 lines of the turn beside two type definitions, and every turn change, 50 commits in a
  week, would still be a change to the file the daemon modules import `JarvisRuntime` from.
- **One `turn.py`** — about 2,400 lines with imports, and every canary and test seam that names the
  waiter, the cancel seams or the driver would name the same path; a change to one is a change to
  the file the others pin. ADR 0339 rejected the same shape for the daemon.
- **Split `drive_turn` in the same change** — it rewrites the lines the existing tests exercise, so
  a green run no longer shows that the move changed nothing; with a pure move the unchanged tests,
  imports apart, are the acceptance evidence.
- **Re-export the moved names from `jarvis.runtime`** — a test that patches a re-exported name on
  the package patches only the alias; the moved code reads its own module's attribute, so the patch
  is silent. CLAUDE.md also rules out a compatibility layer.

## Consequences

- Log lines from moved code carry the new module's logger name, under `jarvis.runtime.turn`: a
  level set on `jarvis.runtime` still applies by inheritance, a filter attached to that logger no
  longer sees them.
- `turn/` does not import the daemon, which imports it. The wait keeps its own 10 ms poll default
  beside the daemon's, two constants of one value.
- The private names that tests, scripts and the daemon had imported from `jarvis.runtime` are
  public in the module that owns them, which widens what the turn exposes.
- A patch of `render_response`, `mark_trigger_consumed` or `time` for the driver targets the
  driver's module, not `jarvis.runtime`.
