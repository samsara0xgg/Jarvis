# ADR 0142 — A Past Day Has One Derived Summary That Recall Returns

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- `recall` answers "what did we talk about on a day" with one line per record:
  118k characters for 1528 records on 2026-10-02. A rolling summary
  (ADR 0134) covers only what the prompt window drops, and it is one text for
  the whole history, so a question about one past day finds neither a short
  account of that day nor a pointer to its records.
- A summary is a model's account, not evidence, and it can be wrong; `records`
  is the transcript that is never flagged or rewritten.
- The Mac sleeps and restarts; a job tied to one clock minute misses days, and
  history before the first deploy has no summaries at all.

## Decision

Write one summary per local calendar day, once a day after a configured time,
for every past day that has records and no summary yet, append it to
`day_summaries` (the latest row of a day is current, older rows stay as
history), and have `recall` return the current summary of each day in its range
before the lines. Today is never summarised. The records stay the source of
truth: a summary that fails its gates (missing heading, over the size cap, cites
a record id that is not that day's) is not stored and is tried again on the next
run (asked once more in the same run first). A day with under 1500 characters of
records is stored word for word as its entry, with no model call: a summary of a few
lines is no shorter than the lines.

## Alternatives rejected

- **Extend the rolling summary with per-day sections** — one chain means a bad
  fold corrupts every later day, and a day cannot be re-written alone; the
  rolling summary already took 3 chunked calls for 1080 records.
- **Summarise on demand inside `recall`** — a paid call on the latency path of
  a voice turn; `recall` today answers in one tool round trip of about 2.6 s.
- **Summarise today at the end of the day** — the day is still being written
  until midnight, so a row written then is stale by the next record; a past day
  is closed.

## Consequences

- The summary rows are derived data: nothing may read them as fact where a
  record can be read, and `recall` points back to ids through the lines.
- A day whose summary keeps failing its gates costs two paid calls per run.
- Summaries ride the first `recall` page and count against its budget, so a
  range of many days leaves less room for lines on that page.
