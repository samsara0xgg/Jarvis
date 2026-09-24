# ADR 0038 — Plugin connections are user-started workflows

**Status:** Accepted
**Date:** 2026-09-23
**Supersedes:** 0032

## Context

Allen approved the Resonance connection storyboard: a conversation opens a
plugin panel, an explicit Connect action authorizes setup, and a successful
connection continues the original request. ADR 0032 permits only a terminal
command to open OAuth; ADR 0035 loads plugins and skills once at boot. A
daemon restart would interrupt the conversation. A newer user request or a
cancelled connection must not revive an old task. Renderer code must not
receive OAuth tokens or rewrite the hand-maintained YAML configuration.

## Decision

Make plugin connection a runtime-owned workflow requested by conversation
and started by an authenticated desktop action (or the existing login CLI),
publishing tools and skills only after successful setup, with continuation
limited to the original still-current request and at most once.

The daemon never initiates a browser login during startup. Desktop controls
use a narrow Electron bridge and a local management credential. UI settings
override plugin defaults in a separate runtime file; credentials stay in
private runtime storage. Only locally available compatible plugin packages
are offered for connection. Closing the panel does not cancel the workflow;
Cancel invalidates its completion and continuation. Existing action approval
rules apply after connection.

## Alternatives rejected

- **Parse assistant prose to open UI** — streamed or reworded prose cannot
  identify one request across renderer reconnections or distinguish a
  suggestion from an actual panel request.
- **Restart after every connection** — disconnects the same websocket that
  must report success and loses the in-progress interaction.
- **Use only tool_search for discovery** — an unauthenticated server has
  registered no tools, so cannot be found by that index.
- **Edit jarvis.yaml from the renderer** — mixes runtime-owned choices with
  operator comments and gives presentation code control of credentials.

## Consequences

Runtime owns connection cancellation, authenticated controls, catalog
compatibility and replacement of the plugin tool group. A process restart
ends pending connection requests rather than replaying authorization.
Changing the conversation before completion suppresses automatic resumption;
the connected plugin remains available. Catalog presence alone does not
imply support for hosted connector gateways or arbitrary plugin components.
