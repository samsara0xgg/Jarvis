# ADR 0135 — The Live User Message Ends with the Reply Language

**Status:** Proposed
**Date:** 2026-10-02
**Supersedes:** none

## Context

- With `reply_language: follow` the prompt tells the model to answer in the
  language of the user's words. Scanning the owner's log (2026-10-02), the
  model still answered in the other language on 7% of turns.
- Replayed against gpt-6-luna with the recorded instructions and history,
  `[Reply language for this turn: English|Chinese]` after the user's words took
  28 wrong samples from 10 wrong to 0, and harmed no zh or en turn.
- A tester asked in Chinese for a web search; the results came back in English and so
  did the answer. After tool results the line in the live message sits far behind them.
  Replayed offline (gpt-6-luna, the owner's full history, a Chinese ask for Rust links,
  three rounds of English results): English answers 5 of 5 with no line, 0 of 6 with the
  line only in the live message, 0 of 5 with a line only after the results. The offline
  replay never failed with the live-message line, so it cannot rank the placements;
  the fix follows the report.
- Live, 2026-10-07 (turn Tb60b06a4): "Show my mail on the Dashboard." called
  `show_on_dashboard`, and the next request ended on the bare line as a user item. The model
  read it as a new, empty user turn and answered the newest open-ended request in the
  history (an answered weather question from 19:25), with nine weather lookups. The first
  request, without the item, had stayed on the mail ask.
- The `follow` rule knows two languages. Japanese words in the same replay were
  forced to Chinese, so a line for them would be wrong.
- Everything ahead of the live message is byte-identical between turns for the
  provider's prefix cache; only the live message may change.

## Decision

The live user message ends, after a blank line, with
`[Reply language for this turn: English]` or `[...: Chinese]`, naming the
language `decide()` already picked for this turn (the `reply_language` setting,
else the language of his words).

Each tool loop, the tool loop and the spoken stream alike, then adds one short user item
after every batch of tool results, so the request that asks for the answer ends with the
line (the late-answer note, which names the language itself, may follow). The item is two
lines: `[Not new words from the user: still answering "<his words>"]`, the words flattened
to one line and capped at 200 characters, then the same language line, so the item never
reads as a new, empty turn. The live message keeps its own line; the line is computed once
per turn, and an item after a batch leaves every earlier request's items as the prefix
of the next one. Only the live message is recorded as sent; the trailing items are not.

Limits: no line when the words contain kana or hangul, or when there are no
words and nothing is pinned. A pinned `en` or `zh` adds the line to every turn,
a system trigger included. The line sits at the end of the live message only,
after the status block's header and the words.

## Alternatives rejected

- **A stronger sentence in the system prompt** — the prompt already says it
  and 7% of turns still mismatched; editing it also moves the start of every
  request, missing the prefix cache once.
- **A line on every history message** — it would rewrite the cached prefix
  for a decision that only matters on the turn being answered.
- **End the last tool result with the line** — equal offline (0 of 5 English),
  but it edits the tool's data, needs the last result found in every loop, and
  moves with each batch, so the earlier request's tail stops being a prefix.
- **The bare language line as the item after tool results** — read as a new, empty user
  turn, it sent the model back to an older request in the history (Context, 2026-10-07).
- **A developer-role item after tool results** — not every configured provider accepts
  one mid-input.
- **Name the language for Japanese and Korean too** — the `follow` rule picks
  `zh` or `en` only, and the replay answered Japanese in Chinese; no language
  is better than a wrong one until the rule knows more than two.

## Consequences

- `record_sent_message` stores the live message as sent, so later turns replay
  it with its line. History then shows the line on every past user turn, and a
  turn whose language the rule got wrong keeps that wrong line in the prompt.
- Each live message is one short line longer on every turn; each tool batch adds one
  more short item to that turn's requests, never to history.
- Japanese and Korean turns still rely on the system prompt alone and keep the
  7% mismatch rate.
