# ADR 0334 — A terminal-only machine sweeps its own logs and recordings, unless a daemon holds its root

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

The owner asked for this fix on 2026-10-21, after the full-repo audit.

ADR 0067 cuts `logs/*.log` at 10 MB and deletes recordings past their days, at boot and then hourly.
That sweep is a task of `serve_inherent`, so it runs only inside a daemon. Since 2026-10-10 the
brain runs on a Raspberry Pi and the owner's Mac runs a terminal and the companion, with no daemon:
`logs/terminal.*.log` and `logs/resonance.*.log` on the Mac have grown since, and the only guard left
is the 50 MB warning of `jarvis daemon status`, which speaks when someone runs it.

What a terminal writes under its runtime root, read from the code: the logs of its agents, and the
recordings of its microphone in `memory.audio_dir` (the capture session's utterances and dictation).
It writes no screenshot (`screen_capture` uses a temporary folder) and no phone attachment (the
brain's). Its other files are small state or a delivery queue with its own lifecycle (the day
recording's outbox, ADR 0256).

A terminal and a daemon can share a root: a one-machine Mac with a terminal beside it, or a dev Mac
running a brain and a terminal. `rotate_logs` copies a log aside and truncates it in place, and
nothing orders two cutters. Every daemon holds `daemon.lock` of its root from before it bootstraps
until it exits (ADR 0003 D4, ADR 0318), so the lock says whether a sweeper is already there.

## Decision

A terminal runs the ADR 0067 sweep over the logs and microphone recordings of its own runtime root,
at boot and then hourly, with its own config; each pass is skipped while a daemon holds that root's
`daemon.lock`.

Limits:

- It is the daemon's function and cadence (`_data_sweep_task`), told only to ask, at every pass,
  whether the root is held. A daemon that stops hands the sweep back within the hour; one that
  starts has it from its own boot.
- It sweeps what a terminal writes and nothing else. `jarvis daemon status` keeps its warning as the
  last guard.

## Alternatives rejected

- **The terminal always sweeps** — on a shared root two cutters can both pass the size check in
  `rotate_logs`; the second then moves the first's `.1` aside and copies the already emptied log
  over it, leaving an empty `.1` and dropping the older kept files.
- **The terminal takes `daemon.lock` for the length of a pass** — a daemon that starts inside the
  pass meets `ProcessLockHeld` and refuses to start, where ADR 0318 has only a real daemon refuse
  a second one.
- **Leave it to the 50 MB warning of `jarvis daemon status`** — it reports only when someone runs
  the command, and the Mac's logs have grown unbounded since 2026-10-10.
- **Sweep screenshots and phone attachments on a terminal too** — it writes neither. The attachment
  folder is paired with the provider's uploaded copies (ADR 0255), which the sweep's release step
  deletes with a key a terminal does not hold: files removed here would orphan their copies.

## Consequences

- Screenshots and attachments left on a Mac that once ran the daemon are not swept by its terminal;
  they stay until removed by hand.
- `process_lock.is_held` takes a free lock for an instant. A daemon that starts in that instant sees
  contention and goes through the stale-holder path, as it already can against the CLI's and
  `daemon status` probes.
- Lines written between the copy and the truncate are lost on a terminal's logs as on a daemon's
  (ADR 0067).
