# ADR 0169 — tool_search searches only deferred tools and names the loaded ones

**Status:** Accepted
**Date:** 2026-10-05
**Supersedes:** none

## Context

- ADR 0127 put five MCP reads on the menu from the first request
  (`tools.mcp.always_loaded`: the Microsoft calendar view and two To Do reads,
  `gmail_search`, `gmail_get`) so a calendar or mail turn would not open with a
  `tool_search`.
- Allen's voice log, 2026-10-02 to 2026-10-05 (`mac_events.db`): 10 of 13 mail,
  calendar and To Do turns still called `tool_search` first. In T7a9d9911 the
  recorded first request already carried `mcp__gmail__gmail_search` and
  `gmail_get`, yet the model searched "Gmail search emails and get full email
  content".
- Each such call is one extra model request, about 2.2 to 2.6 s, and the tools
  it loads change the tool list, so the next request misses the prompt cache
  (ADR 0127 measured 82,100 uncached input tokens after one).
- The model had two reasons to search. `tool_search`'s description lists `gmail`
  as a source and says some tools "may not have been provided upfront", with
  nothing saying which of that source's tools are already callable. And
  `plugin_connections._publish` passed every plugin tool to `build_tool_search`,
  so the BM25 index and the source list covered tools that were already loaded,
  against the "deferred tools only" rule of ADR 0034.

## Decision

`build_tool_search` indexes, lists sources for and returns only deferred tools,
and when a listed source also has non-deferred tools its description names them,
sorted by exact name, as already available to call directly and never to search
for; ADR 0056's read-only siblings still load with the best hit, deferred ones
only.

## Alternatives rejected

- **A line in `jarvis_v1.md` saying those tools are loaded** — paid on every
  turn, including the 341 turns in ADR 0127's log that used no tool, and
  it sits far from the tool whose description invites the search.
- **No `tool_search` for the servers that have loaded tools** — `gmail_send`
  and the other write tools stay deferred (ADR 0034) and only the search finds
  them.
- **Filter at the caller in `plugin_connections`** — fixes the index but not the
  description, and every future caller of `build_tool_search` would have to
  repeat the filter.

## Consequences

The description changes with the loaded set, so editing `always_loaded` already
costs one uncached request (ADR 0127) and now rewrites this tool's text too;
the names are sorted, so the text is stable across calls and the cache holds.
The sentence adds the loaded names to every request. Whether the
model now skips the search is read from the voice log, not guaranteed by this
text.
