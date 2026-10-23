# ADR 0384 — Every Claude Code transcript, on his Mac and in the cloud, is kept raw on the brain

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** none

## Context

- On 2026-10-23 Allen asked whether the raw records behind the daily Claude Code report are kept,
  for both accounts. They are not: ADR 0294 copies each session's metadata, ADR 0303 keeps his
  typed words and Claude's text replies, and both leave out tool calls and results, thinking and
  subagents by design. Cloud sessions (this project's threads, claude.ai/code) are not on the Mac
  at all. He decided that every transcript of both accounts is kept raw on the brain, chose an
  encrypted relay through a private repository for the cloud ones (decision card, 18:46Z), and
  approved raising Claude Code's `cleanupPeriodDays` on the Mac, whose 30-day default had already
  deleted account A's transcripts from before 2026-09-03.
- A transcript is JSONL that Claude Code appends to while a session runs: the session under
  `projects/<folder>/<id>.jsonl`, its subagents under `<id>/subagents/`, tool outputs too large
  for a line under `<id>/tool-results/`. `history.jsonl` in each config directory holds every
  prompt typed there. Not every line has a `uuid`. Measured 2026-10-19: 920 MB of files in 7 days.
- A cloud session runs in a VM that pauses after a few idle minutes and may then be reclaimed;
  reopened, its conversation comes back but the transcript file is written anew. The VM reaches
  GitHub through a proxy that serves only the repositories attached to the session, and never
  the tailnet. The environment's setup script runs as root before Claude Code starts and is kept
  as a filesystem snapshot; no process it starts survives into a session. Claude Code sources
  `~/.bashrc` each time it starts. Which hooks a hosted session runs is documented only for the
  repository's and the organisation's settings, and a project thread has no single repository.
- Transcripts hold tool output, and tool output can hold tokens and passwords; they hold code
  from private repositories and other people's messages. The Jarvis repository is public.
- The raw archives (ADR 0238, ADR 0259) keep bytes whole-or-nothing, refuse at the disk floor
  and sit outside the sweep, "clear recordings" and the export.

## Decision

Every transcript file of both accounts, from the Mac and from cloud sessions, is kept on the
brain byte for byte as gzip pieces of whole lines in the imports archive, and nothing reads it
into a model's context.

- **Pieces.** A piece is the bytes `[start, end)` of one file, ending at a line end for a
  `.jsonl` file; any other file goes whole once it has been quiet. It is kept at
  `imports/claude-code-log/files/<account>/<origin>/<path in the config directory>/<start>-<end>-<sha>.gz`,
  where `<sha>` is the first 16 hex of the SHA-256 of the raw bytes and `<origin>` is `mac` or
  `cloud-<session id>`. The name is the content, so a piece sent again is held, and a file that
  was written anew (shorter, or its first or last shipped bytes changed) starts again at 0 under
  new names. Nothing is overwritten or deleted.
- **What.** `projects/**` and `history.jsonl` of each config directory; links are not followed.
  Not `file-history/`, settings, credentials or caches.
- **Reading.** A reader takes a file's pieces in start order and merges their lines: a line with a
  `uuid` once per uuid, any other line once per exact bytes. The pieces stay as sent.
- **Mac.** The terminal's shipper (ADR 0294) sends each file's new complete lines, and its cursor
  for a file moves only after the brain's digest matches.
- **Cloud.** The environment's setup script installs `scripts/cloud_log.py`. Started from
  `~/.bashrc` and from a SessionStart hook at each Claude Code start, one watcher per VM ships
  when a file has new complete lines and has been quiet 45 seconds, or every 10 minutes while it
  grows: each piece is encrypted with `age` to a key whose secret half exists only on the Mac,
  and the batch is pushed as a new branch `s/<session id>/<stamp>` of the private repository
  `samsara0xgg/jarvis-cloud-log`. The Mac fetches each branch, decrypts, ships the pieces like its
  own, and deletes the branch once the brain holds every piece of it.
- **Boundary.** No day line, no semantic index, no `archive` tool, no prompt. Like ADR 0259's raw
  files it is outside retention, "clear recordings" and the export; only erasing all data
  removes it.

## Alternatives rejected

- **Read cloud sessions back through the sessions API from a scheduled cloud task** — every byte
  passes through a model's context on his quota (a thread's transcript reaches megabytes within
  an hour), and what does not fit is lost.
- **Push the cloud transcripts in plain text to the private repository** — anyone the repository
  is shared with, and GitHub, could read the tokens tool output carries; `age` is already in
  the cloud image and one `brew install` on the Mac.
- **Ship the whole file each time it changes** — a session file grows all day and would be sent
  again every pass, gigabytes a day over the tailnet for megabytes of new lines.
- **Read every line into `import_entries`** — 130 MB a day of JSON into SQLite on the SD card,
  and a transcript line is not a dated entry anybody queries yet.
- **Rely on a Stop hook alone in the cloud** — that a hosted session runs hooks from the VM's own
  user settings is not documented, and nothing would ship until it is shown to; the watcher
  started from `~/.bashrc` does not depend on it.

## Consequences

- The brain stores the transcripts' gzip size for good, from the first pass's backfill of both
  accounts' remaining history onward; the first pass measures it.
- Plain-text tokens that tool output printed now sit on the Pi as well as the Mac.
- A cloud VM written anew is shipped again from its first line: the copies cost space and are
  merged when read. Lines written after the last push of a VM that is then reclaimed are lost.
- Cloud sessions ship only where the private repository is attached; elsewhere the watcher keeps
  trying and loses what it holds when the VM goes. Each project and each account's environment
  needs the setup script and the repository once.
- The Mac's key is the only way to read what waits in the repository; losing it loses what has
  not been relayed.
- What the 30-day cleanup deleted before this is not recovered by it.
