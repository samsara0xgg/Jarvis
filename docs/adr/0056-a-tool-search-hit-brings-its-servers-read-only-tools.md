# ADR 0056 — A tool_search hit brings its server's read-only tools

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- ADR 0034 keeps every MCP tool off the model's menu until `tool_search`
  finds it; the search is BM25 over each tool's name, description and input
  property names, returning the top `limit` names (8 unless the model asks
  for fewer), the way Codex does it.
- Many servers split one job across tools: Google's Workspace server lists
  message ids with `gmail_search` and reads a message only through
  `gmail_get`. `get` is one of the stop words the search drops, so a query
  in the user's words rarely ranks `gmail_get`.
- Live on 2026-09-26 (gpt-5.6-luna), "查一下我 Gmail 里最近有哪些未读邮件"
  searched with `limit: 5`, loaded send, createDraft, search, modify and one
  Microsoft calendar tool, ran `gmail_search` four times, and answered that
  the tool returned only ids.
- A read-only tool runs without a confirmation (ADR 0033), so having one on
  the menu can cost tokens but not an unwanted effect.

## Decision

A `tool_search` result also loads every read-only tool of the server that
supplied its best match; the ranked hits themselves are unchanged, and tools
that need a confirmation still arrive only by rank.

## Alternatives rejected

- **A plugin skill telling the model to call `gmail_get` after a search** —
  the skill is read only if the model decides to read it, which is the same
  step it skipped; the upstream Gmail skill has no such line either.
- **Load the read-only tools of every server among the hits** — in the run
  above the fifth hit was a Microsoft calendar tool, which would have added
  that server's 16 read tools to a mail question.
- **Keep Gmail's read tools always on the menu** — three schemas on every
  turn of every topic, and a per-server exception to ADR 0034 that the next
  search-then-read server would need again.
- **Drop `get` from the stop words** — the failing query did not contain it.

## Consequences

A search whose best match is on a server with many read tools puts all of
them on the menu for the rest of the turn: 16 for the `microsoft` server,
more tokens per call in that turn. A server that marks a write tool
read-only would now get it loaded unasked-for, though still run without
confirmation exactly as before. Which server counts is decided by one rank,
so a stray top hit brings the wrong server's reads.
