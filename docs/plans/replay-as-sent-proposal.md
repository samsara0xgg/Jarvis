# ADR proposal — Each Turn Replays the Messages Earlier Turns Sent

**Status:** Proposed
**Date:** 2026-09-30
**Supersedes:** 0044 (on acceptance; it then gets the next ADR number)

## Context

- ADR 0044's request: `tools`, then `system`, then the memory.db history
  with each earlier `allen` row as its words alone, then this turn's words
  with the state block (time line, channel, cut note, pending ask) at their
  head. Its consequence line expected `system` and the history to stay
  cache-stable.
- OpenAI's prompt cache on gpt-6-luna, probed 2026-09-30 through
  `/v1/responses` with the real prompt (about 44k input tokens: tools about
  5.0k, system about 1.0k, 544 history messages about 36k):

  | Request | Input | Cached |
  |---|---|---|
  | A, first send | 44,098 | 4,674 |
  | A with its last message changed | 44,100 | 4,674 |
  | A plus one more exchange | 44,114 | 44,095 |
  | A without its last 3 messages | 43,956 | 4,674 |
  | the same request sent twice (5 layouts, 2nd send) | 40,278 to 44,026 | all but 3 |

  The cache serves a request through a whole earlier request it starts
  with; a shared prefix that ends inside an earlier request gets only a
  checkpoint of about 4.7k tokens around the tools and the system prompt.
  A `prompt_cache_key` did not change this (0, then 43,941, as without one).
- So each turn's first request misses: the request the previous turn sent
  ended on its words with the state block at their head, and this turn's
  history replays those words without it. About 39k of 44k tokens are
  read uncached on every turn Allen speaks. Wall time for the same request
  uncached then cached: 2.72 to 2.12, 3.46 to 3.31, 3.08 to 1.63, 1.50 to
  1.08, 1.67 to 1.67 s; first token on this prompt was 1.47 s cold and
  1.26 s warm (2026-09-29, 50 calls).
- A shorter history is faster too: the last 12 messages instead of the whole
  history gave first token 1.04 s cold and 0.86 s warm (2026-09-29).
  Nothing older is lost: `search_records` and `read_records` read every row.
- On the spoken route the state block also carries the voice rules (about
  180 tokens); the connected-apps line rides the same block on every turn.

## Decision

Behind `session.replay_sent` (off by default): the user message a turn's
first request sends, state block and words, is kept in memory.db's `sent`
table under the turn's record id, and later histories replay that row as
kept, with no day marker; a row with nothing kept renders as ADR 0044 says.
The block's header reads `[State when this was said | from the program, not
the user's words]`, true both live and in a replay. What every block would
repeat moves to `system`: the voice rules on a spoken-route turn, and the
connected-apps line. `sent` is additive to schema version 1; an older daemon
ignores it.

The shorter history is `session.recent_records` (0, off, by default), which
landed beside this from the voice branch: with N the history carries the
most recent N to 2N-1 rows, the first row shown moves N rows at a time, so
between moves each turn's history starts with the last turn's, and a `user`
line ahead of the rows says how many earlier rows `search_records` and
`read_records` find. It and `replay_sent` compose: a kept row inside the
window replays as sent.

## Alternatives rejected

- **State block after the words, or in a trailing message of its own** —
  the earlier request still ends on it, so the next turn's history must
  replay it to start with that request; it is this decision with a
  different layout, and a trailing `user` message is the adjacent pair ADR
  0044 rejected.
- **State in `system`** — `system` precedes the history; changing it every
  turn leaves only the tools cached (the 4.7k checkpoint above).
- **Chain turns with `previous_response_id`** — it needs `store: true`; the
  request sends `store: false` (`jarvis/decision/llm.py`), so nothing of
  the conversation is kept on the provider's side, and memory.db folds and
  compaction would have to break the chain anyway.
- **`prompt_cache_key` alone** — measured above: it does not make a partial
  prefix hit.
- **Trim the history to a token budget every turn** — its first message
  changes every turn, so no turn's request starts with the previous one.

## Consequences

- Each replayed turn carries its old state block, about 40 tokens: its time
  line dates the row, so no day marker is needed.
- The request after a fold of an unanswered row, a compaction, a profile or
  connected-apps change, or a move of the recent window reuses only the
  checkpoint, once.
- `tool_search` changes the tool list inside a turn, and the tools come
  first: the requests after it in that turn miss. Provider-side deferred
  tools (`defer_loading`) would keep the list fixed; not in this decision.
- Typed turns go through chat completions, which caches separately: a
  switch between typing and speaking misses once each way.
- A card's button writes no row, so its turn keeps no message.
