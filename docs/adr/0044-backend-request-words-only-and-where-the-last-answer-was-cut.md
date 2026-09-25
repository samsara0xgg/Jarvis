# ADR 0044 — The Backend Request: Words Only, and Where the Last Answer Was Cut

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** 0027

## Context

- ADR 0027 (2026-09-21) settled the backend request: rules and profile in
  `system`, memory.db `records` replayed one message per row by role, the
  per-turn state as one block on the last `user` message. Each history line
  kept a `[ts] source:` prefix so the model could tell GPT-Live's words from
  the backend's.
- The local voice chain answers from the backend session directly; the last
  `jarvis_live` row is from 2026-09-15. The voice model and the backend
  conversation model are one model (gpt-5.6-luna since 72f56bd).
- The model copies the prefix into its answers: up to 11 of 14 replies in the
  2026-09-25 replay of Allen's morning session, and again 3 times in that
  day's interruption replays ("[2026-09-25T11:42:05-07:00] jarvis: 好，我继续。").
  Who spoke is already the message role.
- The history still spans days (verbatim since `history_since`, compaction
  not yet triggered), so the model needs to know which day a row belongs to.
- Allen's 2026-09-25 voice test: when he cut Jarvis off, the next turn had no
  trace of it; his asks were to mark where the answer was cut and let the
  model decide whether to continue or take up his new words. The event log
  already carries a validated heard prefix per spoken answer, folded into the
  packet's conversation history.
- Replay of the real session on gpt-5.6-luna with the cut stated in the state
  block: a new question after the cut answered 6/6 without a retelling;
  "你继续说" after a cut factual answer resumed with the unheard part 3/3.

## Decision

The backend request is four parts, in this order: `system` is the rules
file plus the profile block (`[关于 Allen]`, one line per `profile` row,
omitted when empty); the history is the `records` rows after the summary
anchor and on or after `session.history_since`, one message per row with the
role read off `source` (`allen` is `user`, every other source `assistant`),
the text alone with the retired envelope tags and any label an answer copied
into its own text removed, the first `user` row
of each calendar day opening with a `[9月24日 周四]` line, adjacent rows of
one role joined, and the current summary ahead of them as a `user` message;
the last `user` message is this turn's utterance with one
`[当前状态｜程序提供，不是用户说的话]` block prepended, holding the time line,
the interaction mode, where the previous turn's spoken answer was cut if it
was not heard whole (after a quoted heard prefix, before any word, or with no
answer at all), a pending confirmation ask if one is live, and a one-line
count of actions still running; a history that ends on an unanswered `allen`
row folds that row in ahead of the block; `tools` is the registry surface for
`jarvis_llm`. No other note enters the request. Records before
`history_since` stay in memory.db for `search_records` / `read_records`. The
backend is not asked for `<voice>`/`<document>`.

## Alternatives rejected

- **Keep the `[ts] source:` prefix** — measured above: the model writes it
  into answers, and the only reader it served, a Live turn in the history,
  has not happened in ten days.
- **A timestamp on every row without the source** — the copied shape is the
  line prefix itself; one date line per day on a `user` row gives the day
  and puts nothing at the head of the model's own messages.
- **Mark the cut inside the interrupted `assistant` message** — tried on the
  same replay: the marker after the answer and the answer cut down to the
  heard part both did no better on "继续讲" after a cut story (the model
  continues past the story's end in every variant), and cutting the answer
  lost facts the model needed to resume a factual answer.
- **The per-turn state as its own `user` message** — every turn would carry
  two adjacent `user` messages; a provider that merges or rejects them
  changes behaviour silently.
- **The history as one text block in the first `user` message** — the model
  reads its own answers as reported speech; the rows already are the
  conversation.

## Consequences

- Live's words, if a Live session runs again, are indistinguishable from the
  backend's in the history; the Live startup brief keeps its own labelled
  lines.
- "继续讲" after a cut story still continues past the end instead of
  resuming: the history holds the whole written story, and the spoken form
  compresses a story to about 60 characters anyway.
- Tool calls and results are still not in memory.db; the time line and the
  cut line change every turn, so only `system` and the history are
  cache-stable.
