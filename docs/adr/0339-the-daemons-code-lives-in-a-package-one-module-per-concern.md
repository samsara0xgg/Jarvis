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
- `serve_inherent` itself was 1,100 lines of boot, voice wiring, route dependencies, background
  tasks and teardown in one function. Its teardown reads state that boot fills late (the voice
  subsystem with its microphone owner and GPT-Live, the power observer, the watcher list), and
  the order it stops things in is a contract (ADR 0006, 0009), so the phases cannot be cut
  apart without a place for that state to live.

## Decision

Daemon code lives in `jarvis/runtime/daemon/`, one module per concern, and new daemon code goes
in the module that owns its concern, not in `inherent_loop.py`.

- `inherent_loop.py` holds only `serve_inherent`: the order the daemon's phases start in, and
  the call that stops them in the order ADR 0006 and 0009 fix. A phase that grows is split into
  its own module, not into the function.
- State that a phase fills and the teardown or a mute switch reads late lives in a holder the
  phases share (`Voice`, `Running`), not in a closure over `serve_inherent`'s locals. A step that
  starts tasks extends the holder's list in place, so a step that fails mid-way leaves what it
  started to the teardown.
- A name another module imports is public. No module re-exports another's names, and none reads
  a moved name under an alias.
- A module's logger is `logging.getLogger(__name__)`.
- A move changes no behaviour: the code is moved verbatim, and the tests that exercise it patch
  the module that now reads the name.

## Alternatives rejected

- **Top-level modules in `jarvis/runtime/`** — the directory already holds 62 files, and the
  daemon's modules read one another's public names; a package shows which of them are the
  daemon's and keeps them reviewable as one unit.
- **One file with section markers** — a marker gives a group no seam to patch and no path for a
  canary to pin: three canaries named this file's path to check one watcher or sweep function
  each, and a change anywhere in the 8,312 lines was a change to the file they pin.
- **Moving `serve_inherent` and the commentary path in the first change** — it rewrote lines two
  owners were editing, so each edit would have conflicted with the move; both moved last, once
  those owners had finished.

## Consequences

- A test that patches a moved name on `inherent_loop` patches nothing the code reads; the moved
  code reads its own module's attribute, so the patch is silent. The test has to patch the
  module that reads the name.
- Log lines from moved code carry the new module's logger name, so a log filter on
  `jarvis.runtime.inherent_loop` no longer sees them; `serve_inherent` itself logs nothing.
- `Voice` and `Running` are mutable and filled in boot order. A phase that reads a field before
  the phase that fills it has run sees `None`, which the type says and no ordering check enforces.
