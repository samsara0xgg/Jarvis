# ADR 0289 — Her one web search takes a depth, and a deep search is Anthropic's own search in one nested request

**Status:** Accepted
**Date:** 2026-10-17
**Supersedes:** none

## Context

- `web_search` answers from one keyed backend (Tavily), one search per call.
  For a choice, a comparison or a price, one search is thin: a one-search
  answer can rest on a single page.
- On 2026-10-16, on the Pi, Sonnet 5.5 (effort low) answered the same
  questions in two ways. One used Tavily and the other Anthropic's server
  web search (`web_search_20260209`, dynamic filtering). The two tied on
  quality. Anthropic's search was slower (median 11.5 s against 7.7 s) and
  cost about twice as much. At low effort it searched only once per question.
  Its dynamic-filtering step adds about 3 to 4 s and about 6k input tokens
  per search.
- One of those answers spoke the model's scratch note between searches
  ("r is a string; fetch directly.") as part of the answer.
- A wide probe (max_uses 15, told to search widely and cross-check) ran 4
  searches in parallel, took 21.2 s, cost $0.097, and gave clearly the best
  answer of the runs.
- The owner accepts a longer wait for a search that needs it (「如果是让他查东西
  的话，我稍微多等一会儿可以接受」). He chose one tool on which she picks the depth
  herself (「可以」, 2026-10-17), over two tools.
- Only Anthropic's host runs the server web search. The public build's
  first users will run domestic models.

## Decision

Give `web_search` a required `depth` of `quick` or `deep`, chosen by the
model on each call. A quick search stays on the keyed backend. A deep
search is one nested request to Anthropic's server web search on the
`tools.web.deep_preset` preset, allowed `tools.web.deep_max_uses` searches.
That request returns a written brief and its sources as the tool's result.

- Only the text after the nested response's last search or code block is
  the brief. Text before or between searches is the model's working notes.
- When the deep preset is not Anthropic on Anthropic's host, a deep search
  runs the keyed backend at its result cap. The result says it was quick.
- A deep dispatch says the fixed line 「我认真查一下，大概二十秒」 in place of the
  model's own line before the call, because the line carries the wait.

## Alternatives rejected

- **Two tools, a quick one and a deep one.** Two near-identical
  descriptions on one menu invite the wrong pick, and the depth becomes a
  choice of name rather than a field the model must fill on every call.
- **Anthropic's web search as a server tool on the brain's own request.**
  At the brain's effort (low) it searched once per question in the A/B, so
  it was no deeper than Tavily. Every result page also enters the turn's
  context, about 6k tokens per search, and its scratch notes reach the
  spoken answer.
- **Anthropic's web search for every search.** It tied Tavily on quality at
  1.5 times the latency and twice the cost (the A/B above).
- **A deep search as background work that reports later** (as ADR 0254's
  removed deep pass did). The wide probe answered in 21 s, short enough to
  wait for within the turn, so a second channel for the answer buys nothing.

## Consequences

- A deep search costs about 10 cents and 20 s, against about 2 cents and 8 s
  for a quick one. Whether she picks the depth well rests on the tool's
  English description, which the tool exam checks.
- The deep brief is written by a second model call, so the brain answers from
  a summary. A fact the brief left out is not in the turn unless she fetches
  a source.
- The search fee ($0.01 per search) is not in the cost events, which count
  tokens. The result carries the search count.
- On a domestic model a deep search is a wider quick search, not a
  cross-checked one.
