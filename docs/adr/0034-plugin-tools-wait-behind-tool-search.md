# ADR 0034 — Plugin tools wait behind tool_search

**Status:** Accepted
**Date:** 2026-09-23
**Supersedes:** none

## Context

- Every registered tool's name, description and JSON schema is sent on every
  decision call. GitHub's hosted server alone lists 45 tools; Notion's
  documents about 37. Each plugin adds its whole list to every request,
  including turns that never touch it.
- Codex registers every MCP tool as deferred when its provider supports tool
  search: the model first sees one `tool_search` tool (BM25 over each tool's
  name, description and input property names, default limit 8), and a result
  makes the found tools callable on the next request.
- Codex's loading happens server-side on the Responses API; Jarvis's presets
  speak Chat Completions, which has no deferred tools, so the client must add
  the found schemas to `tools` itself.
- The decision loop built its tool list once per turn, before the first call;
  it allows five tool iterations per turn.

## Decision

Register every MCP tool as deferred and, when any exists, one read-only
`tool_search` tool that returns the best-matching names under
`loaded_tools`; the decision loop rebuilds the tool list before every call
from the caller's surface, adding the tools a `loaded_tools` result named for
the rest of that turn.

## Alternatives rejected

- **Send every plugin tool on every call** — 45 GitHub schemas ride along with
  a question about the time; each further plugin grows every request, which
  the provider prices per input token.
- **Keep found tools across turns** — history is replayed as one message per
  row without tool calls, so the next turn's model never sees the search that
  loaded them; a tool on the list with no visible reason is the surprise
  deferral exists to remove.
- **An embedding index** — needs a model call per search and per boot; BM25
  over names ranked GitHub's `issue_write` first for "create an issue" and
  `list_issues` second for "list my open issues" in a probe on 2026-09-23.

## Consequences

A plugin action costs one extra iteration of the five, for the search. A
deferred tool called by exact name without a search still dispatches, as in
Codex. A query made only of stop words finds nothing. Loading is limited to the
caller's surface, so a GPT-Live turn cannot load a write tool.
