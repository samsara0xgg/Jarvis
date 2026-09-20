# ADR 0021 — Reuse TimeSink as the local app activity source

**Status:** Accepted
**Date:** 2026-09-20
**Supersedes:** none

## Context

- Allen chose local integration before MCP. TimeSink already captures app/window
  spans, Chrome URLs, and suspends counting for idle, lock and sleep.
- TimeSink owns a live SQLite WAL database. Existing span rows are extended by
  heartbeats and can be shortened when idle is detected, so append IDs alone
  cannot identify immutable observations.
- The source does not persist suspension reasons or collector health. Empty
  intervals cannot distinguish absence from stopped or failed collection.

## Decision

Read the existing TimeSink database through Jarvis's activity tools without
copying the collector or writing to its database; bind external evidence to the
source database and row revision, rejecting changed evidence explicitly.

Keep TimeSink responsible for collection and local time accounting. Keep screen
capture, scheduler, MCP exposure and inferred project attribution outside this
integration. Interface and failure semantics belong in docs/spec.html.

## Alternatives rejected

- **Build another foreground/idle collector in Jarvis** — two independently timed
  collectors would disagree about span boundaries and duplicate existing native
  permission handling without supplying screen content.
- **Import each new row ID once** — misses later heartbeat extensions and idle
  backdating of that same row, yielding incorrect durations.
- **Add an MCP server first** — the current consumer is Jarvis in the same account;
  another transport does not solve mutable source revisions or missing coverage.

## Consequences

External row updates may require a query restart or leave an older saved source
reference unavailable; this adapter does not archive prior TimeSink revisions.
Database replacement invalidates previous references. Jarvis depends on the
TimeSink span schema and reports an unavailable source on incompatible databases.
Activity gaps remain unexplained until TimeSink persists state/health events.
