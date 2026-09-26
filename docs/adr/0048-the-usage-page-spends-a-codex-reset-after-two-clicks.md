# ADR 0048 — The Usage page spends a Codex reset after two clicks

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** none

## Context

ADR-0018 built the usage observer to read only. Codex and Claude both hand out limit
resets, a few at most and each one-time, and until now spending one meant going to that
product's own client. On 2026-09-25 Allen asked to spend them from the companion's Usage
page, with one condition: "reset如果要实施一定要两句确认 和codex目前的一样 防止误触".

Codex's client is open source (openai/codex, codex-rs/backend-client
`rate_limit_resets.rs`). It posts `{redeem_request_id}` to
`/backend-api/wham/rate-limit-reset-credits/consume` with the same login `collect_codex`
already reads, and gets back reset, nothing_to_reset, no_credit or already_redeemed. The
request id is an idempotency key: the same id sent twice spends one reset. Its terminal
flow is a question ("Use this reset?") answered on a second, separate choice.

Claude Code claims its resets through `/api/organizations/{org}/reset_rate_limits`, but two
of the claim's fields are known only from the Claude Code binary, and reading them was
refused by this session's permission check, even after Allen said go.

The daemon's HTTP routes listen on localhost with no credential, so any local web page can
POST to them. The desktop already holds a private credential (ADR 0038, `plugin-access.json`)
that only Electron's main process reads.

## Decision

Spend a Codex reset only through `POST /inherent/usage/codex/reset`, behind the desktop
credential and a UUID request id. The page mints that id once per question, and asks with
two separate buttons in two places, the second one live only after 600 ms. Claude resets
stay read-only until the claim's fields are known from a source this project may read.

## Alternatives rejected

- **One click with an undo toast** — a spent reset cannot be given back, so there is nothing
  for Undo to do. Allen asked for two confirmations.
- **Call chatgpt.com from the renderer** — the renderer would need the Codex access token,
  which today never leaves the daemon; the credential route keeps it there.
- **Leave the route open like `/inherent/usage/refresh`** — a web page in any browser on this
  Mac could POST to it and spend the reset; refresh only costs a poll.
- **Wire Claude from guessed fields** — a wrong `program` value fails, or claims under another
  program, and it can be tested only by spending the one reset that exists.

## Consequences

The observer is no longer purely a reader: `usage_observer.py` holds one write, and the
desktop credential now guards spending as well as plugin management. A retry after a lost
answer shows "That reset already went through", which is correct but reads like a failure.
Resets have no event of their own; the next `usage.state_observed` shows the lower count.
Claude's resets still need Claude Code (`/rate-limit-options`) to spend.
