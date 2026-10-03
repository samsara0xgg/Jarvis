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
- The `follow` rule knows two languages. Japanese words in the same replay were
  forced to Chinese, so a line for them would be wrong.
- Everything ahead of the live message is byte-identical between turns for the
  provider's prefix cache; only the live message may change.

## Decision

The live user message ends, after a blank line, with
`[Reply language for this turn: English]` or `[...: Chinese]`, naming the
language `decide()` already picked for this turn (the `reply_language` setting,
else the language of his words).

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
- **Name the language for Japanese and Korean too** — the `follow` rule picks
  `zh` or `en` only, and the replay answered Japanese in Chinese; no language
  is better than a wrong one until the rule knows more than two.

## Consequences

- `record_sent_message` stores the live message as sent, so later turns replay
  it with its line. History then shows the line on every past user turn, and a
  turn whose language the rule got wrong keeps that wrong line in the prompt.
- Each live message is one short line longer on every turn.
- Japanese and Korean turns still rely on the system prompt alone and keep the
  7% mismatch rate.
