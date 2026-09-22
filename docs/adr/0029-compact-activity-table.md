# ADR 0029 — Activity queries answer as one compact, code-rendered table

**Status:** Accepted
**Date:** 2026-09-21
**Supersedes:** none

## Context

On 2026-09-21 (turn T1796b684) Allen asked which WeChat contacts he had
talked to and how long NetEase Cloud Music had been open. The decision model
(deepseek-v4-flash, 1M-token window) made five requests and seven tool calls
and then failed with an internal English error. The failure was structural,
not the model's: `query_activity` returned one JSON object per row with
eleven repeated fields (source, provider, kind, project, project_basis,
observed_at=null, detail_available=true, ...), a 300-character summary and a
70-character revision-pinned reference, capped at 50 rows and an 11,000
character page. A day holds about 1,400 app spans and 700 screen captures:
that is more than a hundred pages, so the first page showed 13 rows of a
window the model then tried to page through, narrowed, and re-queried,
spending every tool iteration on transport.

The tool supported no app filter and no duration total, so "how long" could
only be answered by paging every span and adding durations by hand. A cursor
reused with different arguments failed with a message blaming a changed
TimeSink row. The same data is consumed by the work-state and daily-report
gatherers through `jarvis.state.timesink`, whose item shape they depend on.

## Decision

`query_activity` returns one deterministic page: a header stating the shared
facts once (date, UTC offset, store identity, an app dictionary with letters,
per-app totals over the whole window, coverage and TimeSink state) followed
by rows `[ref, start, end, app, text]`, filled to a character budget of
48,000 (rows before state events, both paged by one cursor, nothing dropped).
Row references are the database row id plus the first eight hex digits of
the row revision (`s<id>:<rev>`, `c<id>:<rev>`, `g<uid>`); `read_activity`
accepts them, and the full saved reference is the page's store plus the row
ref. The tool gains an `app` substring filter and `summary_only`. Cursors
issued for other arguments fail as `cursor_mismatch`; changed rows stay
`invalid_cursor`. The character budget is a budget of serialized JSON, not
tokens; the spec records the caps. The compaction lives in the tool layer
(`jarvis.state.daily_activity`); the adapter's item shape is unchanged so the
work-state and daily-report gatherers are untouched.

## Alternatives rejected

- **Raise the row limit and the caps, keep the per-row JSON objects.** A
  day of 2,100 rows at the measured 700-1,000 characters per row is 1.5-2
  million characters; even at a 64,000-character cap that is 25-30 pages,
  and the model would still add up durations itself.
- **A model-written summary of the rows.** Adds a request to every activity
  query, costs latency and money, and the summary cannot be checked against
  the store; the totals this ADR computes are checkable with one SQL query.
- **Per-query short ids (r1, r2, ...) in a server-side table.** Needs a
  persistent id map keyed by query, expiry, and a rule for what `r1` means
  when two queries are alive in one turn; row ids with an 8-hex revision are
  already unique and pin the revision without any new state.
- **Full 32-hex revisions on every row.** Twenty-four extra characters per
  row, about 50,000 per day, for a collision probability the 8-hex prefix
  already puts below 2^-32 per changed row.

## Consequences

Every consumer of the old item shape through the tool (tests, the tool
descriptions, the spec paragraphs) changes at once; the item shape inside
`jarvis.state.timesink` is frozen by two other gatherers and must not be
compacted in place. A row's seconds-precision clock loses the stored
milliseconds; the detail reader keeps them. `read_activity` on a short ref
resolves against the configured store, so a store replaced with identical
rows reads the same content under a new identity: only full references
carry the identity check. Saved references may now be 8 hex digits long and
are matched by prefix. A page of 48,000 characters can be 45,000 tokens of
CJK text: the budget is bounded, not small, and callers that want only
durations should ask for `summary_only`.
