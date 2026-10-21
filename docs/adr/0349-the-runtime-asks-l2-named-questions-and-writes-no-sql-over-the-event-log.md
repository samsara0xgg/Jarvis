# ADR 0349 — The runtime asks L2 named questions and writes no SQL over the event log

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

The 2026-10-20 audit of the composition root (main db6a2636, findings F9 and F6) found the
runtime reading the event log in its own words. Re-measured on main cfb1b636:

- `jarvis/runtime/__init__.py` and `inherent_loop.py` held 23 SQL string literals between them.
  They named 12 payload keys (`$.turn_id`, `$.phase`, `$.reason`, `$.endpoint_reason`,
  `$.confirmation_id`, ...), the `correlation_json` column and the order of the table's columns.
  `_hydrate_event_row` decoded an `events` row a second time, beside L2's own decoding.
- `import-linter` reads imports, not strings. A payload key or column that L2 renames breaks
  these queries without any gate noticing: the runtime keeps working and answers wrongly, and
  only a test that happens to drive the path shows it.
- Two parsers answered "how did this turn end": the Live delegation (`_turn_outcome`) and
  `POST /inherent/ask` (`ask_outcome`). Run over the 20 shapes of log in
  `tests/integration/test_turn_outcome.py` they agree on 16. On the other 4 the Live parser called
  the turn failed while its answer was still to come: a wait line taken back, a wait line that
  failed, a streamed run another run took over, and, when that second run then failed itself, it
  reported the first run's reason in place of the real one. `ask_outcome` counts only the turn's
  own rows, and the spec of `/inherent/ask` (`docs/spec.html#local-endpoints`) already says a wait
  line and its being taken back are not the answer.
- Nine more runtime files outside those two hold SQL (`device_voice`, `phone_voice`,
  `picture_files`, `picture_notes`, `plugin_connections`, `push`, `spend_cap`, `day_plan`,
  `day_summary`). The daemon package (ADR 0339) holds none.
- `jarvis/state` is organised by concern: 48 of its 84 modules run their own SQL, each for its
  own concern, and none collects queries by kind.

## Decision

`jarvis/runtime` asks L2 named questions and writes no SQL over the event log: every query on
`events`, with its columns and its payload and correlation paths, lives in `jarvis/state` as a
function that says what it answers.

- Rows come back decoded by L2's one `row_to_event`, as `Event`, or as the scalar, set or small
  record the question asks for. A cursor is `latest_id` and `fetch_events_after`.
- A question lives in the L2 module of the concern it asks about, beside the writers of the
  events it reads, as `jarvis/state` is already organised; there is no module of queries.
- There is one `turn_outcome`. Only the turn's own rows count: a wait line (`commentary`) and a
  streamed run another run took over (`corrects_response_id`) are neither its answer nor its
  failure. Where the two older parsers disagreed, the stricter, the ask path's, is the rule;
  each caller maps the answer to its own shape and keeps its own wording of a failure's reason.
- A canary, `tests/canary/test_canary_runtime_writes_no_sql.py`, fails on SQL text in any
  `jarvis/runtime/**/*.py` and counts the files that still hold it. The count only goes down,
  and the canary names no file.

## Alternatives rejected

- **A `jarvis/state/queries/` package of every query** — it would hold a question away from the
  module that owns the events it reads, so a payload rename in `turn_steps` would again need a
  grep across a second place; 48 of 84 state modules already keep their reads beside their folds.
- **A lint rule banning `sqlite3` or `.execute` in `jarvis/runtime`** — 35 runtime files import
  `sqlite3`, nearly all for the `Connection` type and `sqlite3.Error`, so a ban on the import is
  red on code that runs no query; and ruff has no ratchet, so a ban on `.execute` is red on the
  nine remaining files today and goes green only through per-file ignores, the list of names the
  next bullet refuses.
- **An allowlist of the files still holding SQL** — ADR 0325: a canary's rule sits with the
  thing, not in a list of file names. A list also lets a tenth file join unnoticed by renaming
  one; the count does not.
- **Keep both outcome parsers and document where they differ** — they disagree on 4 of 20 shapes
  of log and the difference is a fault, not a policy: the Live path told the model a turn had
  failed while the turn's answer was still being written.
- **A typed record for every payload an L2 question returns** — `_fetched_mail` and
  `_calendar_time` read keys of a tool's output, and that shape is L4's. Mirroring it in L2
  would put L4's contract in the wrong layer, so those questions return the `Event`.

## Consequences

- The canary counts nine files, the ones listed above. Each lowers the count in the commit that
  moves its queries; the count includes SQL over `memory.db` (`day_plan` reads `day_prose`),
  which the canary cannot tell from the event log's.
- `row_to_event` and `EVENT_COLUMNS` are public, so any L2 module can select its own rows. That
  every question is named stays a review matter.
- L5 keeps its observers' queries, and the media owner its drain of the log by row id (it decodes
  with `row_to_event`); this decision covers the runtime only.
- A Live delegation whose wait line is taken back no longer reports failure: it stays running
  until the answer or a failure of the turn's own run lands. When a `turn.failed` carries no
  reason, the Live path still shows its `exception_repr` and `/inherent/ask` its event type.
- The moved queries are word for word the old ones, so they read through the same indexes.
