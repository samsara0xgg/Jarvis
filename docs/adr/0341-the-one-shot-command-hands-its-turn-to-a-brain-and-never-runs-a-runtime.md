# ADR 0341 — The one-shot command hands its turn to a brain and never runs a runtime

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0002 § Daemon / CLI contract, the regex classifier, the ack-before-fork and the in-process turn of `python -m jarvis "<utterance>"`; in ADR 0009 D2, the fall-through to that in-process turn and `--no-forward` (ADR 0003 D4's refusal). The forward to a daemon, B-NEW-5 and `--brain` stand.

## Context

The owner chose this on 2026-10-21, after the audit's investigation of the command.

ADR 0002 made `jarvis "<text>"` assemble a whole runtime in the invoking process: bootstrap the
root, open its event log, run the turn, and for an utterance the regex `跑|spawn|…|run` matched,
print an ack and fork-detach first. ADR 0009 D2 made the command a client of a daemon when the
root's `daemon.lock` is held or the `com.allen.jarvis` plist exists, and left the in-process turn
as the fallback. Since 2026-10-10 the brain is a Raspberry Pi (ADR 0170) and the Mac runs a
terminal; the fallback is now the common case on one machine and a hazard on the other.

Probed on this tree with `HOME` in a tmp dir, `run_turn` replaced by a recorder (no model call):

- On the Mac there is no lock and no daemon plist. `jarvis --no-detach hello` ran its turn on the
  root's existing `mac_events.db`, which held the events of an earlier daemon, and
  `bootstrap_runtime_app` also opened a root whose `daemon.lock` was held, with no refusal. Nothing
  checks that this machine is a brain; a turn there is answered from the memory of 2026-10-10 and
  its rows land in that log.
- On the Pi the brain unit has `Restart=always`, `RestartSec=10`. `is_agent_installed` looks for a
  plist and is never true on Linux, so in every restart window the lock is free and the command runs
  a second runtime on the brain's root. It holds no lock, so the brain starts and takes its own
  (ADR 0318 protects `serve` against `serve` only); two runtimes then write one log, and the
  orphan-turn consequence ADR 0009 D2 describes applies.
- The classifier matches `run` as a substring: "what is running on port 8006" is long-run.

Nothing calls the in-process turn: the LaunchAgents, the systemd units, the Electron app, the
scripts and the README start `serve`, `terminal` or `check-log`, and spec.html documents only
`--brain`. Its callers are a live-LLM canary, a refusal test for `--no-forward` and an AST canary on
the fork; `fork_detach` has not changed since 2026-05-18.

## Decision

`python -m jarvis "<utterance>"` hands the turn to the `--brain` given, else to the daemon on this
machine, and never assembles a runtime in its own process; with no brain to hand it to, it exits 3
with a hint to pass `--brain URL`.

Limits:

- The forward to the daemon on this machine, B-NEW-5, `--brain` and `--brain-token-file` are
  unchanged. `--no-detach`, `--no-forward`, `--prompt` and `--config` of the one-shot are gone;
  `serve` keeps its own `--config` and `--prompt`.
- The command opens and writes nothing under any root.
- `bootstrap_runtime_app` and `install_power_observer` stay: `serve`, the voice surface and the
  tests run on them. `run_turn` did not: neither `serve` nor the voice surface ever called it (they
  run on `drive_turn`, which the daemon's intent pump drives), its one caller was a live test, and
  that test now emits its intent and calls `drive_turn`; `run_turn` is deleted.

## Alternatives rejected

- **Keep it** — the probe above shows a turn on a stale log on the Mac and a second runtime in the
  Pi's restart window, and no launchd, systemd, Electron, script or doc path starts it.
- **Keep it behind a guard** — refusing where `runtime.role` is `brain`, or where the lock is free
  but a daemon is expected, still leaves a second composition of the runtime that nothing calls,
  and the Mac has no signal that its brain is elsewhere: its terminal's brain URL is only an
  argument in the terminal plist.
- **Default `--brain` to the terminal's brain** — that needs a stored URL and a reader for it; the
  flag is documented and already works.

## Consequences

- A script that passed `--no-detach`, `--no-forward`, `--prompt` or `--config` to the one-shot now
  gets an argparse error (exit 2). Whether Allen has such scripts is not visible from the repo.
- In the Pi's restart window `jarvis "…"` says there is no brain instead of answering; the old
  forward's three one-second retries covered that window only where the plist exists.
- The tests that drove the in-process turn are gone with it
  (`tests/canary/test_runtime_root_isolation.py`, `tests/canary/test_canary_daemon_ack_before_fork.py`,
  and the `--no-forward` refusal test).
