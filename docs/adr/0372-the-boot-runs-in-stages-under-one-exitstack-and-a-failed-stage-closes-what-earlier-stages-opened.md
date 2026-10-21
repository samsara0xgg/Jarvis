# ADR 0372 — The boot runs in stages under one ExitStack, and a failed stage closes what earlier stages opened

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

Read from the code path on this tree, not from a plan.

`bootstrap_runtime_app` was one 500-line function in `jarvis/runtime/__init__.py`, a file of 4,600
lines that also held the feature builders it calls and the turn code. It opened the Event Log
connection first, started the plugin connections (an MCP loop thread and a child process per
server) inside `register_catalog`, and only then ran the checks that refuse a boot: the Tier 0,
grammar and cue tables, the `llm` section and about forty `RuntimeBootstrapError` raise sites in
the feature builders. None of those closed the connection or stopped the plugin connections.

A caller that outlives a failed boot (a test booting runtimes in one process, the terminal)
kept the SQLite handle and the MCP children. Two of the leaks had no owner to release them: the
plugin connections are returned by `register_catalog` only after job mail and the home are built,
so a refusal in that window left running connections nobody held; and the geocoding thread of
kept places was started before about twenty steps that can still fail, and cannot be called back.

Constraints: the exception types, error texts and defaults are the tests' and the owner's
interface (ADR 0370), so a failed boot must raise the exception it raised, not a wrapper. The
runtime owns the Event Log connection and the plugin connections once the boot succeeds, so they
must not be closed on the way out of a good boot. `runtime/` is the one place that wires across
layers, and `lint-imports` is unchanged by splitting inside it.

## Decision

The boot is a sequence of stage functions, each in a module of `jarvis/runtime/bootstrap/`,
that `bootstrap_runtime_app` runs under one `ExitStack`: a stage that opens something the runtime
will own hands the stack its close at the moment the thing exists; a failure in any later stage
closes them in reverse order and propagates unchanged; a boot that builds the runtime releases the
stack.

- What cannot be undone happens after the last step that can fail: the geocoding thread starts
  once the boot has returned a runtime, and the stale-files handler is set when the runtime is
  built.
- The process settings a boot keeps in module state (language, model-call log, `open_path`
  bookmarks) are set in one function, before anything is opened. A failed boot does not restore
  them; the next boot sets them again.

## Alternatives rejected

- **A `try/except` at each raise site.** The resource belongs to the function that opened it, not
  to the builder that raises, so every one of the forty-odd sites would have to know what is
  open, and a new site would leak until someone remembered. The stack has two registrations.
- **One `try/finally` around the old function body.** It closes the Event Log, but the plugin
  connections are not visible to the outer function until `register_catalog` returns; a refusal
  inside it (job mail, the home) still leaks. Registering them inside the stage that creates them
  covers that window.
- **Nested `with` blocks per resource.** A `with` closes on the good path too, and the runtime
  needs the connections open; `ExitStack.pop_all` is the hand-over. Nesting also grows one level
  per stage.
- **Close in the callers.** The connection is created inside the failed call and never returned,
  so no caller has it.

## Consequences

- A stage that opens a connection, thread or child process must register its close on the stack;
  a review should catch an `open_*` or `start()` in a stage with no callback. Nothing checks it
  but the failure test, which covers the Event Log connection and the plugin connections.
- A close that itself raises replaces the boot's exception in the traceback's foreground
  (`ExitStack` chains the original as its context). None of the registered closes raises in
  practice.
- The memory store has no connection to unwind: its version check opens and closes it in place.
- Stages hand each other frozen results (`Core`, `Features`, `Llm`, `Intakes`), so a new service
  is a field on one of them.
- The moved code logs under `jarvis.runtime.bootstrap.<module>` instead of `jarvis.runtime`; a
  filter on the old name still sees them through the hierarchy, a filter on the exact name does
  not.
