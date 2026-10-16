# ADR 0287 — On Anthropic's host the deferred tools wait for Anthropic's own tool search

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** none

## Context

- ADR 0034 keeps deferred tools off the menu until Jarvis's own `tool_search`
  finds them. Its result comes back as a tool result, so a found tool is
  called one model request later, and the loop adds the found schemas to
  `tools` for that request.
- `tools` leads Anthropic's cache prefix (tools, then system, then
  messages). A changed tool list therefore misses the cache for the whole
  prompt: ADR 0169 measured 82,100 uncached input tokens after one search,
  and each extra request took 2.2 to 2.6 s. A live turn on 2026-10-16 sent
  about 84k input tokens.
- The brain has run on Claude through Anthropic's own API since 2026-10-12
  (ADR 0253). Anthropic's API offers its own tool search
  (`tool_search_tool_bm25_20251119`, on every Claude model Jarvis uses). The
  request carries every tool, and the deferred ones are marked
  `defer_loading`. The search runs inside the model's response, which then
  calls the found tool. A deferred tool stays out of the cached prefix until
  it is found. The response's `server_tool_use` and
  `tool_search_tool_result` blocks must be sent back on the next request of
  the turn.
- Only Anthropic's host runs that search. Other providers, and other servers
  that speak the Messages API, still need Jarvis's `tool_search`. The public
  build's first users are in mainland China and will run domestic models.
- On 2026-10-16 the owner chose Anthropic's tool search over Jarvis's own on
  this path (「以后找工具就用 Anthropic 自带的了吧」, 「动手工具搜索也用它
  native 的」). He kept Jarvis's own search for other APIs (「原来那一层是不是
  也要留着？以后如果切换成别的 API 就不行了」, 「就相当于我们现在只改
  Anthropic 这条路」).

## Decision

When the decision client's provider is Anthropic on Anthropic's own host,
send every tool on the surface, with the deferred ones marked `defer_loading`.
Put Anthropic's BM25 tool search where `tool_search` stood. Move the source
list from `tool_search`'s description into the system prompt's stable part.
Any other client keeps ADR 0034's `tool_search` unchanged.

- A deferred tool that a tool on the menu has taken over (ADR 0242) is not
  sent, so no search finds it.
- The thinking and server-tool blocks of a tool-calling response go back
  unchanged, in their order, on the next request of the turn. History holds
  neither, so each turn searches again, as ADR 0034's turn-scoped loading
  did.

## Alternatives rejected

- **Keep `tool_search` and answer with `tool_reference` blocks** (Anthropic's
  custom-search form). It keeps the cache, but a found tool is still called
  one model request later, the 2.2 to 2.6 s ADR 0169 measured.
- **Anthropic's tool search for every client** — other providers reject a
  `defer_loading` tool, and a domestic server speaking the Messages API has
  no search to run.
- **Regex variant** — the model writes Python patterns over English
  metadata. BM25 takes the same natural-language queries `tool_search` does
  now.

## Consequences

- On this path ADR 0056 no longer applies. A search returns its top matches
  (5 unless the model asks for more), and a server's read-only siblings come
  only when they match.
- The step frames never see a search here, since no client tool call is made.
- A search is not a `tool_search` call in the event log. It shows only in
  the LLM io log (ADR 0120), in the turn's next request, which carries the
  search blocks back.
- Whether the model finds the right tool now depends on Anthropic's ranking
  over our English descriptions, which we cannot tune. The tool exam is the
  check.
