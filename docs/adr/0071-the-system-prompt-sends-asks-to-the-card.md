# ADR 0071 — The system prompt sends asks to the card

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- ADR 0066 gave the model `ask_user` and left the choice to it: no prompt
  line steered it. The prompt's only asking rule said when to ask the user,
  not how, so "ask" read as a sentence in the reply.
- Allen's first live test, 2026-09-26, asked by voice for an email to a
  friend. Three turns asked for the address, subject and body in words. The
  tool was on the menu every turn.
- Sandbox replays with real plugins and, unless noted, real history:
  - gpt-5.6-luna, no thinking: the email request got a card in 1 of 7 runs,
    3 of them with history removed.
  - gpt-6-luna, no thinking: 5 of 7 requests that needed details got a
    card. Both misses came after a `tool_search`, and both asked in words.
  - gpt-6-luna with low or medium thinking, on 8 lines: the median turn
    slowed from 6.1 s to 9.0 s and 11.1 s. The email request got a card
    either way, and takeout got none either way.
- Codex steers its own ask tool from the prompt. GPT-6's base instructions
  say to use the tool for missing information, and Plan mode's say to
  strongly prefer it. The tool description is one line, and no check runs
  after the reply.
- Allen, 2026-09-26: try the prompt line first.

## Decision

The asking rule in `prompts/jarvis_v1.md` says to ask through `ask_user`
rather than in the reply. It changes nothing else about when to ask.

## Alternatives rejected

- **A small model reads each reply and puts up the card itself.** Offline,
  on 40 real replies, it caught all 5 real asks and flagged 6 more. Three of
  those were send confirmations, which a rule could skip. One was borderline,
  and two asked nothing. It costs a model call after every reply that shows
  no card.
- **Model choice alone** — 5 of 7 on gpt-6-luna. It still asked in words
  after a tool search.
- **Thinking on every turn** — about 3 to 5 s slower per turn at the median.
  Takeout still got no card, and low thinking turned a reminder into a
  calendar question whose meeting length it marked to remember.

## Consequences

- With the line, the same 7 requests got 7 cards in one run each. Three
  replies that needed nothing got none.
- The model still chooses. A miss still comes as a question in words, and
  nothing catches it.
- In that run the hotel card marked the dates and the budget to remember.
  The remember flag is still the model's call.
- A garbled line after an ask is read as part of the ask and gets a card.
  That happened before the line too.
- This is the first prompt line added to steer one tool. The next miss
  should be fixed outside the prompt.
