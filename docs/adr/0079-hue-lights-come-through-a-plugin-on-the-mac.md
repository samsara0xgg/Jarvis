# ADR 0079 — Hue lights come through a plugin on the Mac

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- Allen, 2026-09-26: Jarvis should control the lights now, and a Hue plugin
  should be enough.
- `docs/spec.html` §16.2 names the RPi HOME NODE as the Hue master, with the
  Mac sending `request.hue_control` over MQTT; `docs/architecture-mac-only.md`
  keeps RPi, smart home and cross-domain work out until the Mac loop is
  stable. No HOME NODE, MQTT broker or `cross_domain.*` code exists.
- The bridge (BSB002, five lights, no rooms, one zone) sits on the Mac's LAN
  and answers CLIP v2 with the application key the legacy Pi build registered.
- ADR 0035 plugins already start a stdio MCP server per plugin, with
  credentials from the plugin page or `~/.jarvis/env`; ADR 0033 asks before
  any tool the server does not mark read-only.
- No vendor MCP server exists. Of the community ones, `openhue mcp` (Go, 153
  stars) finds lights only through rooms; `huemcp` (PyPI, one release) reads
  `/resource/light` and zones directly, and its source is 500 lines of GETs
  and PUTs to the one bridge host.

## Decision

Control Hue from the Mac through `plugins/hue`, which runs `huemcp` pinned to
0.1.0 against the bridge on the LAN, until a HOME NODE exists; Jarvis keeps
no light state of its own.

## Alternatives rejected

- **Wait for the RPi HOME NODE of spec §16** — it needs an MQTT broker,
  `cross_domain.*` request and response events and a second daemon, none of
  which exist.
- **`openhue mcp`** — on this bridge `openhue get light` lists zero of the five
  lights, because none is in a room; its config is a file, not the env
  fields the plugin page fills.
- **Port legacy `devices/hue` (phue) as a Jarvis tool** — phue speaks only the
  v1 API, and a plugin needs no Jarvis code.
- **Hue's cloud remote API** — needs an OAuth app registered with Signify and
  sends every switch through the internet, while the bridge is one LAN hop
  away.

## Consequences

Light control stops when the Mac sleeps or leaves home. Moving it to the
HOME NODE later means replacing this plugin with `request.hue_control`. The
server marks no tool read-only, so the package approves its six reads by name
and every change asks first unless Allen sets the plugin to approve. `huemcp`
declares `mcp>=1.2` but imports FastMCP, which mcp 2 removed, so the launch
pins `mcp<2`; a one-person package gets no updates unless someone copies a
new version pin in.
