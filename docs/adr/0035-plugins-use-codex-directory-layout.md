# ADR 0035 — Plugins use Codex's directory layout

**Status:** Accepted
**Date:** 2026-09-23
**Supersedes:** none

## Context

- Allen, 2026-09-23: Jarvis should connect to third-party apps instead of
  rebuilding them, and follow Codex for plugins; Notion first, GitHub and
  Gmail as well.
- A Codex plugin is a directory: `.codex-plugin/plugin.json` (manifest),
  `.mcp.json` (`mcpServers`: name -> server entry, using `url`,
  `bearer_token_env_var`, `http_headers`, `env_http_headers`, `oauth`,
  `oauth_resource`), `skills/<skill>/SKILL.md`, and `.app.json` for
  ChatGPT-hosted connectors. A user's own `mcp_servers` entry outranks a
  plugin server of the same name; per-server approval overrides live under
  `plugins.<name>.mcp_servers.<server>`.
- The Gmail and Notion connectors are not in Codex's repository: the vendors
  host the MCP servers, and a plugin only carries the address, the login
  method and skills. `.app.json` needs OpenAI's connector gateway.
- Codex lists each skill's name and description in the prompt and lets the
  model read `SKILL.md` itself; Jarvis's decision model has no file tool.
- Gmail's hosted server (`gmailmcp.googleapis.com`) is a Workspace Developer
  Preview with no dynamic client registration.

## Decision

Read plugins in Codex's layout from `plugins/<name>/`, switched on by
`tools.plugins.<name>`; merge their `.mcp.json` servers under
`tools.mcp.servers`, which wins on a name clash, understand Codex's server
keys, list their skills in the system prompt, and serve skill files through a
read-only `read_skill` tool confined to each skill's directory. `.app.json`
is ignored.

## Alternatives rejected

- **Only `tools.mcp.servers` entries, no plugin directory** — skills have
  nowhere to live, and each vendor's address and auth style is retyped by hand
  instead of copied from Codex's catalogue of 62 plugins.
- **Load skills into every prompt in full** — Notion's four skills are 17.7 KB
  of `SKILL.md` plus 280 KB of references; the listed catalogue with its
  usage rules is 1.8 KB.
- **Ship a Gmail plugin on Google's hosted server** — it needs Developer
  Preview membership and a pre-registered Web client; Allen's account is a
  personal Gmail address, so the plugin would never come up.

## Consequences

A vendored skill names tools the way its author did (`Notion:fetch`), not
Jarvis's `mcp__notion__notion-fetch`; the model maps them. A plugin whose
server needs a login is skipped at boot until `mcp-login` ran. Updating a
vendored plugin is a manual copy from Codex's catalogue.
