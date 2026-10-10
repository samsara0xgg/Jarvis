# ADR 0201 — The prompt carries a ledger computed from his data

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- Allen asks what he did yesterday, how this week compares with last week, how many applications
  he has sent, when the interview is. The answers are in his own data (TimeSink spans, job mail,
  commits, the daily report, conversation summaries), but the decision model sees only the core
  memory and the conversation, and a tool call costs a hop on a spoken turn.
- The "know-you" eval asked 102 facts about him. With the current prompt, 28 of the 102 were
  present in the prompt before the first token, and a replay answered 11 of 40 correctly. With the
  blocks below, 75 of 102 were present; round-3 replay answered 13 of 32 correctly against 6 of 32
  on the items both runs shared, and the median time to first content fell from 11.23 s to 4.98 s.
- Arithmetic over rows is exact and a model's arithmetic over prose is not. Hours worked, counts
  of applications and the day a week starts on must not be model-made.
- The provider caches the prompt prefix. Text that changes every turn belongs after the history;
  text that changes once a day can sit in the system prompt without costing a cache miss per turn.

## Decision

The prompt carries a ledger of day, week and month facts about him, every number computed by
the program from rows; only the one "The day" line per day is model-made.

- **A. Core memory items carry a note.** Each line shows the date it was first seen (the earliest
  record it cites, else its `since`) and a kind: said, mail, observed, a mix, or set.
- **B-C. Standing blocks, in the system prompt after the core memory.** Recent days (seven in
  full with their conversation summary excerpt and "The day" line, seven more one line each), this
  week so far, last week and the last 30 days. They use complete days only, so the text is the
  same for every turn of a local day and stays in the cached prefix. It is rebuilt when the day
  turns, at 05:00 (a working day that ran past midnight stops at its real end, which is known
  only then; before 05:00 it shows as stopping at midnight), and when a new summary or "The day"
  line lands.
- **D-F. Per-turn blocks, after the time line.** The job hunt as of now (every job mail so far,
  so an application confirmed this morning is already in the count; it reads only memory.db and
  is about 1k characters), today since midnight (computer, job mail, interview changes, mail
  screened, reminders) and what happened since he last talked to her.
- **"The day" is written nightly** in the day-summary task, after the summaries and the core
  memory, by the flex tier, for each of the last seven complete days that has activity and no
  line yet. It is stored per day in `day_prose` and gated like a summary (not empty, not cut off,
  not too long, no markdown). A transient failure is retried; a day that cannot be written stays
  empty and the block is shown without it.
- **The prompt tells the model** that these blocks are newer than `[About the user]` and that it
  answers from them without a tool.
- **The ledger never breaks a turn.** A failure while computing a block is logged and that block
  is left out.

## Alternatives rejected

- **Let the model compute from tools.** A `job_ledger` or `timesink` call per question costs at
  least one hop; the eval's median first content was 11.23 s without the blocks and 4.98 s with
  them, and the replay got 6 of 32 right where the blocks got 13.
- **Model-written weekly and monthly summaries.** A model summarising a week of prose repeats and
  invents totals; the numbers in the blocks match arithmetic over the rows exactly, which a
  summary of summaries cannot promise.
- **Put every block in the per-turn state.** The standing text is about 12k characters; sending
  it after the history on every turn makes it uncacheable, and it does not change within a day.
- **Include today in the standing text.** Today changes every minute, so the cached prefix would
  miss on every turn; D and E keep it after the time line. A product render with the job hunt
  frozen at midnight answered "how many have I applied to" two short on a day with new
  confirmations, which is why D moved out of the standing text.

## Consequences

- The system prompt grows by about 12k characters, and the provider's cache misses once a day
  when the text is rebuilt (and once when a new summary or "The day" line lands).
- The standing text takes about 0.8 s to compute once a day, in the first turn or the prefix
  warm that needs it; the per-turn text takes about 60 ms every turn.
- "The day" costs a fraction of a cent a day.
- A day with no TimeSink spans reads as "no recorded activity", which is what the data says and
  not necessarily what happened.
- The blocks are as accurate as the job-mail classification and TimeSink's project verdicts they
  count from.
