# ADR 0031 — MCP servers are tool sources

**Status:** Superseded-by-0033
**Date:** 2026-09-22
**Supersedes:** none

## Context

Every Jarvis tool is a flat `Tool` (ADR 0019): a synchronous function of its
arguments plus a JSON schema, registered once at boot so the L3 menu can list
it. Capabilities outside this repository (files, GitHub, time, browsers) are
now published as MCP servers rather than as Python packages, and the official
`mcp` SDK (2.x) reaches them only through an asyncio `Client` whose context
manager must be entered and exited from the same task. L4 owns no event loop
and must not borrow one from another layer. Tool names cross into the
OpenAI-compatible chat API every preset speaks, which accepts only
`[A-Za-z0-9_-]{1,64}`. `confirmation_threshold` is `L3`.

## Decision

Every entry under `tools.mcp.servers` is entered once at registry build on a
private asyncio thread owned by L4, and each tool the server lists is
registered as a flat `Tool` named `mcp__<server>__<tool>`; the handler hops
onto that thread, and the server's `readOnlyHint` alone decides `read_only`
with risk `L0` versus `L1`. A server that cannot be entered logs a warning
and contributes no tools; it never fails boot.

## Alternatives rejected

- **Lazy connect on first call** (the Codex app-server pattern) — the menu is
  built from the registry at boot, so a tool the server has not yet listed
  can never be called; there is no first call to connect on.
- **Pin `mcp<2`** — 1.x has no high-level `Client`; importing the 1.x
  `FastMCP` path under 2.x raises an error naming the migration guide, so the
  branch is already closed upstream.
- **A per-server risk knob in config** — both mapped levels sit below the
  `L3` confirmation threshold, and so would every level a knob could pick
  short of `L3`; the knob would change nothing at the gate.
- **Jarvis as an MCP server** — nothing on this machine consumes one; the
  request is to reach servers, not to be reached.

## Consequences

Every runtime boot, including a one-shot CLI turn, spawns every configured
stdio server and waits for its tool list, up to `tools.mcp.timeout_s` per
server. A server that dies after boot stays on the menu until restart; each
call then returns an `mcp_server` error observation. A call blocks the
dispatching thread for up to the timeout plus a fixed grace. Non-text
content (images, audio, resources) reaches the model by type only.
