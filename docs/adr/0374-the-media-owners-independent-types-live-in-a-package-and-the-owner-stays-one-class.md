# ADR 0374 — The media owner's independent types live in a package; the owner stays one class

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- `jarvis/surface/voice_media.py` held the persistent media owner in 4,250 lines. Its one class,
  `StreamingTTSPipeline`, was 3,495 of them: 111 methods and 77 fields set in one `__init__`. The
  rest is the config reader, the outcome and error types, the registry of delivered responses, the
  response and active-response records and the resampler. None of them reads an owner field; the
  owner reads them.
- The class holds ten concerns (the actor thread, the stop of a player or provider, the rows and
  frames around an output, the spare session, the `say` fallback, how a playback ends, power, the
  streaming into the player ring, the lane, the controls). They share the 77 fields and call one
  another: 37 of the 111 methods are called from a concern other than their own, in 89
  caller-callee pairs.
- The voice-surface audit's R2 and R3 propose the decomposition of the owner: the durable rows
  behind a journal port off the actor's loop (R2), and the power controller and the response
  streamer as collaborators that own their own state behind explicit interfaces (R3).
  That is a change of behaviour on the live voice path, where the order of steps, the timeouts and
  the log and error texts are contracts (ADR 0006, 0008), and it is not this decision.
- A test that drives the owner patches the module attribute the code reads, so where a name lives
  is part of its test seam. 19 integration test files and 3 support modules import the module;
  tests patch `emit_event`, `terminalize_playback`, `asyncio.create_subprocess_exec` and one
  constant on it, and two canaries read its source by path.
- The six layers are enforced by `lint-imports`. A split inside `surface/` changes no layer
  contract. ADR 0339 and 0371 did the same for the daemon and the turn.

## Decision

The media owner's code lives in `jarvis/surface/voice_media/`. What is independent of the owner's
state is a module of its own; the owner stays one class in one module, `pipeline.py`, until it is
decomposed into collaborators that own their state.

- `config`, `contract`, `registry`, `response` and `pcm` hold what the owner is configured with,
  answers with and keeps. They import no owner code, and the owner imports them.
- `StreamingTTSPipeline` and the constants only it reads move together, its methods in their
  current order. Nothing is split out of the class, and no base class, mixin or declaration stands
  in for a method that stays in it.
- A name another module imports is public. No module re-exports another's names. A module's
  logger is `logging.getLogger(__name__)`.
- A move changes no behaviour: the code is moved verbatim, and the tests that exercise it patch
  the module that now reads the name.

## Alternatives rejected

- **The class split across modules, as base classes each extending the one before it, or as
  mixins** — every part still shares the 77 fields and calls the others (89 pairs), so a reader
  needs the method resolution order to follow a call, and every call that goes up the chain needs
  an abstract declaration to type-check. It moves lines without moving the ownership of any state, and
  it stands in the way of the decomposition above: a mixin boundary is not a collaborator
  boundary, so the next change would undo it first.
- **A module of functions per concern, each taking the owner as `pipeline`** — the 89 cross-module
  calls become imports between modules, and the lane, the streaming and the terminal call one
  another, so they become an import cycle; the `self` in 111 bodies is renamed, so the moved text
  is no longer the old text.
- **Collaborator objects now (audit R2, R3)** — each would hold the 77 fields or a port over them,
  and the power state machine's lock and attempt ids would cross it. That rewrites the lines the
  existing tests exercise, so a green run no longer shows that the move changed nothing.
- **Re-export the moved names from `jarvis.surface.voice_media`** — a test patch of a re-exported
  name patches only the alias, while the moved code reads its own module's attribute, so the patch
  is silent. CLAUDE.md also rules out a compatibility layer.

## Consequences

- `pipeline.py` stays about 3,600 lines until the owner is decomposed; this decision does not
  make the class smaller, only what surrounds it.
- Log lines from the owner carry the logger name `jarvis.surface.voice_media.pipeline`: a level or
  a handler on `jarvis.surface.voice_media` still applies by inheritance, a filter attached to it
  no longer sees them.
- A test that patched `emit_event`, `terminalize_playback` or the spare's age bound on
  `voice_media` patches `jarvis.surface.voice_media.pipeline`.
- The private names that tests or another module use (the response buffer, chunk and active
  response, the media command, the fade and yield-ramp times, the spare's age bound) are public in
  the module that owns them.
- The canary on `_stream_with_prefix_fallback` reads `pipeline.py`, the hot-path canary lists the
  package's modules, and the canaries that enumerated `voice_*.py` also enumerate
  `voice_media/*.py`.
