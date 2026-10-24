# ADR 0383 — The media owner is a shell over collaborators that each own their state

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** none

## Context

- `StreamingTTSPipeline` in `jarvis/surface/voice_media/pipeline.py` is 3,597 lines: 111
  methods and 77 fields set in one `__init__`, driven by one actor thread, `jarvis-media-owner`.
  ADR 0374 moved what is independent of its state out of it and left the class whole "until it
  is decomposed into collaborators that own their state". It measured ten concerns that share
  the 77 fields, with 37 methods called from a concern other than their own (89 pairs).
- The voice-surface audit (2026-10-20, R2, R3, R5) proposes that decomposition. Allen approved
  stages 0–4 of the audit on 2026-10-21, this one among them.
- On the live voice path the order of steps, the timeouts, and the log and error texts are
  contracts (ADR 0006, 0008). A reader cannot tell from a green suite that a rewrite kept them;
  a move of one concern at a time, with the old lines inside the new owner, can be read.
- Some fields are written from any thread (the gain from the capture thread, the shutdown
  deadline from `close`), some only on the actor's loop, and the power state machine from the
  host's sleep notifications under its own lock (ADR 0014 D20).
- A first attempt split the class into a chain of ten classes, each extending the one before,
  with abstract stubs for the calls going up the chain. Every class still read and wrote the
  shared fields.

## Decision

The media owner is a shell over collaborators, each the only writer of its own fields; the
shell keeps the thread, the loop, the command queue, startup and the public facade.

- **OutputGain** (any thread): the mute and yield gain and the fades around a cut.
  **ShutdownClock** (any thread): the deadline that only moves earlier, what is left of it,
  and the bounded wait of a task.
- **PlayerStopper**: the bounded stops of the player and the provider, and the shutdown
  cleanup record. **PowerController**: suspend, resume, admit, abort and revoke; it reaches the
  loop only through a port that submits to it, and marks shutdown inside its own lock.
- **PlaybackJournal**: owns the actor's connection and every row it writes or reads.
  **SpareSessions**, **NetworkLostCue**, **RingSink**, **SayFallback** (with its janitors) and
  **ResponseStreamer**, which returns an outcome rather than failing the active response itself.
- **PlaybackLane**: the registry, the cursor, the active, held and parked responses, the
  terminal debt and the foreground decision.
- A collaborator is composed, not inherited, and is given only what it needs. Each move is one
  commit that changes no behaviour, in the order above with the lane last; a test that patched
  a name the moved code reads patches the module that now reads it.
- The journal stays on the actor's thread: its calls keep their await points, and an await in
  it does not yield to another task. Moving it to a thread of its own changes behaviour and
  waits for its own decision, on evidence that SQLite stalls the actor.

## Alternatives rejected

- **A chain of base classes, or mixins** — tried and rejected: every link still shares the 77
  fields, a call up the chain needs an abstract stub to type-check, and a reader follows the
  method resolution order instead of an interface. No field gets a single writer.
- **One rewrite in a single commit** — the 89 cross-concern calls move at once, so a red test
  or a changed log line cannot be bisected to a concern, and the diff cannot be read as a move.
- **The journal on its own thread now (audit R2 in full)** — every row's write becomes
  asynchronous to the playback it records; ADR 0006's order of terminal before successor would
  need a new proof. Nothing on the real logs shows a SQLite stall of the actor yet.
- **Collaborators that receive the whole owner** — each could still write any field, which is
  the coupling this removes; mypy could not show who writes what.

## Consequences

- A field read by two collaborators is read through the owner's interface, not shared; where
  the actor needs two collaborators to agree (the lane and the journal at a terminal), the
  shell orders the calls.
- Tests that read the owner's private fields read the collaborator's public state instead, and
  some move to the collaborator's own level.
- The canaries that read `pipeline.py` by path follow the method they guard into its module.
- The owner's log lines keep their texts; their logger names become the collaborators' modules.
