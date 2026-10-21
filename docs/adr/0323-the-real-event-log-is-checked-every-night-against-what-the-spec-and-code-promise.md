# ADR 0323 — The real event log is checked every night against what the spec and the code promise

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

A hermetic test builds the world it tests. The audit of the owner's real logs since 2026-08-25
found 69 bugs, five times what the whole suite found, and 11 % of them came from the real
environment (the Pi against the Mac, real devices, real data), which no hermetic world contains.
One example: on the Pi, which has no `afconvert`, every `utterance.received.audio_artifact_ref`
named a `.m4a` while a `.wav` was written, so every audio ref dangled. Every test passed.

The log already holds the evidence. It is append-only; every response, action, playback and
confirmation ends in one compare-and-set terminal (ADR 0008); every model request has one
`cost.recorded` row; a due reminder is fired or cancelled. Each is a promise of the spec or the
code that a query can check.

Constraints: the brain's log is hundreds of thousands of rows on a Pi's SD card that the daemon is
writing; it holds the owner's words and possibly a key; the check must also run on the Mac's
backup copy, days after the copy was taken; and it may not take the daemon lock or write.

## Decision

`jarvis check-log` runs a fixed set of invariants as read-only queries over the last few days of a
runtime root's event log, and a systemd timer on the brain runs it nightly and keeps the dated
JSON report under `logs/`. It reports invariant names, counts, event ids and times, never payload
text.

Limits: the window ends at the log's newest event, not at the wall clock. The log opens
`mode=ro`, and `immutable=1` when no `-wal` file shows a writer. A run, action or playback is
"not closed" only after 20 minutes. The clock is checked only on event types the brain stamps
itself. Every statement is a SEARCH on an existing index.

## Alternatives rejected

- **More hermetic tests of the same promises** — they pass in any world the author can imagine:
  the dangling audio refs passed the whole suite. The 69 findings came from the owner's log at five
  times the suite's rate, and a test cannot observe a host it does not run on.
- **A window back from the wall clock** — a backup copy checked a week after it was taken has no
  rows in a two-day window and passes, or judges every run that was open at the copy's last moment
  as unclosed. With `ref = now` instead of `min(now, newest ts)`, the tests of a copy fail.
- **Always `mode=ro`** — on a closed WAL database SQLite creates `-shm` and `-wal` beside it, so a
  read-only check changes the directory listing and fails on a read-only mount. Reproduced by
  `test_it_changes_nothing_on_a_log_nobody_has_open`. `immutable=1` is unsafe with a writer, which
  is why the open mode follows the `-wal` file.
- **Copy the log with the backup API, check the copy** — a full write of the log to the SD card
  every night, for a check that reads a two-day window of a 300,000-row log in 0.24 s.
- **The clock invariant on every event** — a terminal's relayed events (`phone.location_observed`)
  and `night.*` carry their own timestamps and would read as clock steps. The invariant covers the
  17 types whose emitters pass none.
- **A `--out` flag, so the CLI writes the report** — one more CLI surface with one caller. systemd
  has no date specifier for the file name, so the unit's shell redirect (to a `.tmp`, then renamed
  so the backup never copies half of one) keeps the CLI to stdout and an exit code.

## Consequences

- The invariants are a second list of the lifecycle owners' terminal sets. A new terminal type added
  there and not here reads as an unclosed run; a test pins the sets against the owners.
- A finding is visible only while it is inside the window (2 days by default): a night missed for
  longer than that leaves a gap unless `--since-days` is raised. `--since-days` must also stay under
  `memory.audio_retention_days`, or a deleted recording reads as a dangling ref.
- A violation is reported up to 20 minutes late, and a secret-shape finding names the shape, not
  the secret: the owner opens the event by id to see what to rotate.
- The audio check needs the recordings, which the Mac backup does not copy (only `*.db` and the
  reports travel). On a copy it is `skipped`, with the reason, not passed.
- The unit exits non-zero on a violation or an unopenable log; nothing else tells the owner. The
  report reaches the Mac with the next 04:00 backup.
