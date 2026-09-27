# ADR 0068 — Jarvis refuses data a newer Jarvis wrote

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

Once Jarvis ships as an app, a user can install an older build over a newer one. On
2026-09-26 Allen accepted that a newer Jarvis upgrades old data by itself, and an older
one that meets newer data refuses to open it and says "update Jarvis" rather than
damage it.

What the code did before:

- `open_event_log` migrates by column presence and then stamps `PRAGMA user_version = 2`
  unconditionally, on every open. A file a later build had moved to 3 would be stamped
  back to 2 and written in the old format by old code.
- `memory.db` had no version at all (Allen's reads 0), so a later format change could
  not be told apart from today's.
- `open_runtime_event_log` already refuses any version other than 2, but it runs after
  the boot open has stamped the file.

## Decision

Every Jarvis database carries its format in `PRAGMA user_version`; the opener migrates a
lower version up, and raises `NewerDataError` for a higher one without writing, which
makes `jarvis serve` exit with code 3.

## Alternatives rejected

- **Open newer files read-only and run degraded** — every path that writes (a turn, an
  event, a remembered fact) would fail one by one mid-conversation, and the user would
  learn about the version mismatch from broken answers instead of one message.
- **A version file beside the databases** — `memory.db` and the event log can be copied,
  exported or restored one at a time; a version inside each file travels with it, and
  SQLite already reserves `user_version` for exactly this.
- **A migration framework (ordered steps, a history table)** — `memory.db` goes from 0 to
  1 with no schema change and the event log's one migration already exists; a framework
  would have one entry.

## Consequences

Builds from before this change (the 0.1.0 DMG among them) still stamp a newer event log
down, and they cannot be fixed after the fact. Exit code 3 is only a signal: the app must
read it to show "update Jarvis", which waits for the error-state design. Launchd's
KeepAlive restarts a refused daemon every 10 seconds, and each refusal writes one line.
