# ADR 0095 — The agent host opens with a key of its own

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- The agent host (ADR 0073) answered only requests that carried the daemon's
  local token, `plugin-access.json` in the runtime root, which the daemon
  writes (`docs/spec.html#local-endpoints`). On a Mac where the daemon has not
  run, or does not start, every request from the window got 401 (Startrail
  release audit, A3, 2026-09-29).
- That token also opens the daemon's own routes, and the host's `/term` hands
  out a login shell (`docs/spec.html#other-openings`).
- The host outlives the companion: it keeps running when the companion or the
  daemon restarts, and a new companion finds it by its port
  (`docs/spec.html#process-map`).
- The runtime root is 0700 and belongs to the one owner of the macOS account
  (ADR 0090).
- Companions built before this change send the daemon's token, and Allen
  switches between builds on his Mac (`try-companion`, `try-companion back`).

## Decision

The agent host accepts a key of its own: 32 random bytes in
`<agents folder>/host-key`, readable only by the owner, made by whichever of
the host and the companion needs it first. The daemon's token still opens the
host, for companions built before this change.

## Alternatives rejected

- **Keep the daemon's token alone** — the window then needs a process it
  otherwise has no use for: with no daemon, every request fails (A3), and
  Startrail cannot ship on its own.
- **The companion hands a fresh key to the host it starts** — the host
  outlives the companion, so a restarted companion would hold no key for the
  host already on the port and would have to kill it, ending its Codex turns;
  a file both read survives either restart.

## Consequences

- Two secrets open the host and its `/term` until the daemon's token is
  dropped from it, which is a later change.
- A host from before this change refuses the new key; the companion then
  replaces it the way a restart does (its Claude turns carry on in the keeper,
  ADR 0082; its Codex turns end).
