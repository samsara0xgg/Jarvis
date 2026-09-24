# ADR 0032 — Remote MCP login is a foreground command

**Status:** Superseded-by-0036
**Date:** 2026-09-22
**Supersedes:** none

## Context

Remote MCP servers authenticate two ways: a fixed bearer header the operator
already holds (GitHub's hosted server), or OAuth 2.1 with dynamic client
registration and PKCE, which needs a browser once and a refresh token after.
Jarvis's runtime is a launchd daemon with no TTY that starts at login and is
respawned after every crash; a browser tab it opened would appear at an
unpredictable moment or on no screen at all. The SDK's provider reloads
stored tokens without their expiry, so a restarted process presents a stale
access token as valid, and the 401 that follows starts a full authorization
flow instead of a refresh. Secrets never enter `jarvis.yaml`; `~/.jarvis/env`
(0600) is the existing indirection. Dynamic client registration records the
redirect URI, so the loopback callback port is part of the registration.

## Decision

A remote entry authenticates with `headers`, whose values are `$VAR`-expanded
from the daemon environment, or with `auth: oauth`, whose tokens and client
registration live in one 0600 file per server under `<runtime root>/mcp/`
together with an absolute expiry the provider is seeded with, so a restart
refreshes rather than re-authorizes. Only `python -m jarvis mcp-login
<server>` may open a browser, on a fixed loopback callback port; the daemon
skips an OAuth server whose token file is missing or no longer usable and
logs that command.

## Alternatives rejected

- **Browser at daemon boot** — launchd starts the daemon at login and after
  every crash with no TTY; each unattended start would open a tab or sit in
  the flow for `timeout_s`, and a revoked token would repeat that on every
  KeepAlive respawn.
- **Device-code flow (RFC 8628)** — the SDK ships no poller, and the daemon
  has no surface to show the user code on; Hermes hand-rolled one and still
  starts it only on an explicit `--flow device`.
- **Ephemeral callback port** — dynamic client registration stores the
  redirect URI; a new port on every login invalidates the stored client and
  forces re-registration, which servers that cap registrations refuse.
- **Tokens in `jarvis.yaml` or `~/.jarvis/env`** — refresh rotates them; a
  file the SDK rewrites cannot be the file Allen edits by hand.

## Consequences

Every OAuth server needs one interactive login on this Mac before the daemon
can use it, and a daemon restart after. A revoked or non-refreshable token
drops the server from the menu until the next login. Changing the callback
port means logging in again. Non-loopback redirect URIs, the device-code
flow and SSE-only servers stay unsupported.
