# ADR 0134 — The Recent-Records Window Decides What Is Folded into the Summary

**Status:** Proposed
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Every request carried all history since `history_since`: 1384 records and
  about 73k tokens over 18 days on 2026-10-02, 1.96 s median time to first
  token. `session.recent_records` = 120 (the latest 120 to 239 records) cut
  it to 0.87 s, but anything before the window is gone from the prompt.
- No summary has ever been written, so nothing covers what the window drops.
  Compaction waits for an hour of idle, a verbatim history past 40% of the
  1.05M context, and records older than 7 days; at 73k tokens the size gate
  never opens. Far-reference probes at N=120 without a summary lost 3 of 6
  (a demo date 120 records back, a sentence from that morning, a number from
  the night before); full history lost 0 of 6.
- The existing summariser, its nine required headings and the `summaries`
  table already do the folding, and `render_context` already shows a summary
  followed by the records after its anchor.
- The first fold has the most to read: 1080 records, 160k characters, 94k
  tokens, on a summariser limited to 200k tokens a minute.

## Decision

When `session.recent_records` is above 0, the records the window hides are
folded into the summary by the existing summariser as soon as they exist,
in the background after a turn and one job at a time; the idle, size and
7-day gates apply only when it is 0.

Limits: the new anchor is the last hidden record, so the prompt is the
summary and the window with nothing between them. A failed or rejected fold
keeps the old summary and is retried after the next record, not every tick.
A fold input over 200k characters is cut into runs, each extending the
summary the run before wrote.

## Alternatives rejected

- **Lower `compact_at_context_ratio` and `verbatim_window_days`** — both
  gates are time or size, not the window: a 7-day verbatim span is about 500
  records and 40k tokens (2026-09-30), against 120 to 239 records, so the
  request stays several times larger than the window's.
- **The window alone, no summary** — 3 of 6 far probes lost at N=120, and a
  fact survived only when the cut left 128 records instead of 120.
- **A dated per-day section for the last 7 days** — the nine headings are
  checked by name and the summary already dates every fact and its record
  id; a tenth required heading would change the contract every stored
  summary was written under, with no probe that a day-by-day layout answers
  better.
- **Fold one chunk ahead of the window, before its cut moves** — the window
  would then be shorter than N records right after every fold, and the
  trial's fold took 51 s against a window that moves every 120 records.

## Consequences

- The summary is rewritten about every N records, so the prompt's start
  changes then and one request misses the provider's prefix cache.
- Between the window moving and the fold landing (51 s for the first fold,
  2026-10-02) the prompt shows the old summary and the note that N records
  are not shown; a turn in that gap cannot see them without searching.
- The summary adds about 5k tokens (median time to first token 0.99 s with
  it, 0.92 s without, 2.29 s for all history, same hour).
- A summary drops detail: probes for the first sentence of the morning and
  for a number said the night before still failed, and one answered with a
  wrong figure. Exact wording is a `search_records` and `read_records` call,
  which the summary's header says.
- `summary_max_chars` rose from 8000 to 20000 and the prompt's target to
  9000: luna wrote 9628 characters for 18 days under an 8000 cap and was
  rejected, then 13455 in the first live fold under a 12000 cap; its retry
  landed at 8314. A fold costs one summariser call, about 0.01 USD.
