# ADR 0127 — Calendar, To Do and mail reads skip tool_search

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- ADR 0034 keeps every MCP tool off the menu until `tool_search` finds it, and
  ADR 0056 makes the best hit load all of its server's read-only tools.
- Allen's voice log, 2026-09-29 to 2026-10-01 (`mac_events.db`, 7 calendar and
  6 mail turns): a calendar question took a median 14.8 s and a mail question
  12.5 s from his words to the end of the answer, against 3.0 s for the 341
  turns that used no tool. The calendar turns spent a median 2 calls before
  `get-calendar-view` (`tool_search`, then `list-accounts` and `list-calendars`
  that the 18 loaded tools invited); the real call came at a median 9.0 s.
- The slowest turn (T318f467d, 26.6 s): the five model requests took 17.8 s,
  `list-calendars` 6.8 s, and the `tool_search` request grew the tool list from
  32 to 50, so the next request read nothing from the prompt cache (82,100
  input tokens, 0 cached, against 71,757 cached before it).
- A single-account server needs no account id, the turn's state block carries
  the local time and offset, and the three calendar and To Do reads are about
  11.5 KB of schema (about 3k tokens) against a 72k-token request.

## Decision

`tools.mcp.always_loaded` lists MCP tools, by `mcp__<server>__<tool>`, that
are registered without `deferred` and so stay on the menu from the first
request; the shipped list is the Microsoft calendar view and To Do reads and
Gmail's `gmail_search` and `gmail_get`, and a name matching no connected tool
does nothing.

## Alternatives rejected

- **Tell the model in the prompt to skip `list-accounts`** — the model reaches
  `list-accounts` only after `tool_search` has loaded it; without the search
  there is nothing to tell, and a prompt line is paid on every turn.
- **Per-server `always_loaded` in the server entry** — the Microsoft server
  lives in Allen's own `settings.yaml`, so a shipped default could not reach
  it, and a plugin's `.mcp.json` would need the same key repeated.
- **Cue-based preload from the transcript** — a keyword list ("calendar",
  "mail") misses the Chinese and indirect phrasings in the log ("我明天有什么日程呀")
  and puts a second router beside the model.
- **Always load every read tool of the server** — 16 Microsoft reads are about
  10k tokens (82,100 against 72,152 input tokens in T318f467d) and bring back
  `list-accounts` and `list-calendars`.

## Consequences

Every request carries these schemas, about 3k tokens for the Microsoft three
plus Gmail's two, read from the prompt cache after the first; editing the list
changes the tool prefix and costs one uncached request. A server that is not
connected adds nothing. The list is kept current by hand: a tool Allen starts
using that is not in it still costs a `tool_search`.
