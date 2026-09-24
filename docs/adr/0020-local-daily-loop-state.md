# ADR 0020 — Keep personal daily-loop state local and separate from worker execution

**Status:** Superseded-by-0036
**Date:** 2026-09-20
**Supersedes:** none

## Context

- Allen approved the daily-loop tool plan and chose local todos before Microsoft
  integration. Personal commitments must remain usable without a cloud task service.
- ADR 0019 delegates engineering execution to Codex. Its worker status cannot
  determine whether Allen's personal commitment is complete.
- The existing event log provides immutable revisions and SQLite transactions;
  conversation records already occupy a separate memory.db, with session compaction
  evolving on another branch. Two writers for the same fact would require synchronization.
- The new flat Tool contract leaves action serialization and lifecycle to the
  dispatcher. Retries and concurrent edits can occur across different action IDs.

## Decision

Keep todos, sourced knowledge and saved briefings as local event-derived records
behind flat tools, using transactional request deduplication and version checks;
query historical observations separately from their collectors and from current-state tools.

Do not revive the engineering Task Ledger, introduce Microsoft synchronization,
change the conversation schema, or let saving a briefing trigger its delivery.
Existing read-only Live delegation remains read-only until its write policy is
explicitly implemented. Interface details belong in docs/spec.html.

## Alternatives rejected

- **Store todos in both Jarvis and Microsoft now** — marking one copy complete
  would leave the other open without an additional synchronization protocol; none
  is needed to demonstrate the first local daily loop.
- **Reuse the engineering Task Ledger** — its worker assignment and verification
  states cannot represent user completion, reopening and postponement without
  reintroducing responsibilities removed by ADR 0019.
- **Store derived knowledge by overwriting conversation history** — correcting one
  conclusion would destroy or mutate its original source; source-ID retrieval must
  still return the original words after a knowledge correction.
- **Let activity queries collect on demand** — a current screenshot or git diff
  cannot reproduce yesterday's screen or repository state, and a historical lookup
  would unexpectedly invoke external processes or a vision model.

## Consequences

Event folds cost time proportional to the relevant local history; materialized
indexes may be needed if measured usage outgrows this implementation. Knowledge
sources can become unavailable if the independent conversation database is removed.
There is no Microsoft sync, notification scheduler or new activity collector in
this change. Historical Git coverage stays unknown without collector health events.
