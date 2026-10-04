# ADR 0149 — The History Is Day Summaries, Then Every Word from the Boundary Day

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- The rolling summary (ADR 0134) is one text for 18 days, rewritten about every
  `recent_records` records; it moves the history's head then, drops detail, and its
  fold is a paid call after the window moves. A past day now has its own summary
  (ADR 0142) and the lasting facts are core memory (ADR 0146), both written once a day
  from `records`, which is never rewritten.
- Live backup of 2026-10-03, request as the decision path sends it (gpt-6-luna, no
  reasoning, `/v1/responses`, streamed, 22 of the 38 live tools): the rolling layout was
  22.7k to 26.3k input tokens (median 24.3k), the layout below 13.9k to 17.6k (median
  15.6k). Median time to first token over 20 utterances, first send / immediate repeat /
  nothing cached: rolling 1.30 / 1.18 / 1.09 s, this layout 0.98 / 1.10 / 0.95 s; p90 1.59
  / 1.47 / 1.48 s and 1.50 / 1.45 / 1.92 s. With every request starting with the last
  turn's (ADR 0044's replay), 96% of input tokens were cached on the first send in both.
  The difference is inside the run-to-run spread of 20 samples; the size is not.
- Eight far-reference questions, one send each, no tools: both layouts answered 5 right
  and gave the same wrong answer on the zip code (9-26 record, in neither summary) and on
  "what I asked you to call me"; the rolling one dropped one of five co-op employers that
  core memory names. Three right answers (9-28, 10-1, co-op) were already in the history
  as answers given earlier that day, so they do not test the summaries.

## Decision

Behind `session.context: day_summaries` (absent or `rolling` is ADR 0134's layout,
unchanged), render the history as: one `user` message of the latest three day summaries
before the boundary day B, oldest first, each as stored, under the header `[Earlier days ·
summaries only · ...]`; a line saying every word from B 00:00 on is below and before that
only the summaries and core memory exist; then every record whose local date is B or
later, rendered as today. B is the day after the newest local day with a day summary; with
no day summary the turn renders as `rolling`. In this mode the rolling summary is neither
shown nor written, `recent_records` is ignored, and the compaction sweep and the window
fold do nothing. If the records from B on exceed `session.context_raw_max_chars` (40000),
the oldest hide in blocks of 50 until they fit and a note says how many and to use `recall`
or `search_records`. The system prompt, core memory last, is unchanged.

Accepted after a comparison on the 20 latest live turns: the median prompt fell from 24.3k
to 15.6k tokens and median time to first token from 1.30 s to 0.98 s (within the spread of
20 samples), with no detail question lost. `rolling` stays available as `session.context: rolling`.

## Alternatives rejected

- **Keep the rolling summary and add day summaries beside it** — the two overlap for the
  same days, and the rolling one is the part that moves the head and costs a fold per
  window move; the sample above showed no question the rolling one answered that the day
  layout did not.
- **A character cap that hides records one at a time** — the head then changes on every
  record once the cap binds, and no turn's request starts with the previous one; a block
  of 50 moves it once per 50 records.
- **More than three day summaries** — every added day is about 1k characters the prompt
  carries each turn for a question `recall` answers from the same summary row in one call.

## Consequences

- A fact from a day older than the three shown and not in core memory is reachable only
  through `recall`, `search_records` or `read_records`: the zip code and the nickname
  request above were answered "no record" with no tools. With tools the model must choose
  to call one.
- A day whose summary job failed leaves B on the day before it, so that day's records ride
  raw (up to the cap) until the summary lands; then the head moves once.
- The head moves once a day, when a new summary lands, and when a block of 50 is hidden or
  the day's records pass the cap; each costs one request's cache.
- The Live startup brief still reads the rolling summary and is not changed here.
- The measurement used a truncated copy of the backup per utterance, the live frame
  (instructions, text schema) copied from the last logged live request and 22 of its 38
  tools; the 16 missing add the same tokens to both layouts.
