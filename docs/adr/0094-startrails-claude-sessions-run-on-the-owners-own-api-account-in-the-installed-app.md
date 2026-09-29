# ADR 0094 — Startrail's Claude sessions run on the owner's own API account in the installed app

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- ADR 0073 runs the Agents window's (now Startrail's) Claude sessions through
  the Claude Agent SDK on Allen's own sign-in, and says a public Jarvis has to
  revisit that. The installed app keeps the window off until then
  (`docs/spec.html#agent-host`, release inventory 改19).
- Claude Code's legal page
  (https://code.claude.com/docs/en/legal-and-compliance, read 2026-09-29):
  - "Developers building products or services that interact with Claude's
    capabilities, including those using the Agent SDK, should use API key
    authentication through Claude Console or a supported cloud provider.
    Anthropic does not permit third-party developers to offer Claude.ai login
    into their own applications, or to route requests through Free, Pro, or
    Max plan credentials on behalf of their users."
  - "preinstalling or running Claude Code in your products or services ...
    requires agreeing to our Commercial Terms of Service", and customers "may
    not remove, disable, or restrict any authentication method built into it
    (including methods that permit signing in with a Claude account or the
    user's own API key)". Each end user authenticates "with their own
    Anthropic API key, Claude subscription plan credentials, or 3P inference
    provider credential (Amazon Bedrock, Google Cloud's Agent Platform,
    Microsoft Foundry)".
  - Nothing there prevents "an end user from signing in to the unmodified
    Claude Code binary with their own Claude subscription".
- Allen, 2026-09-29, on the Startrail audit's decision card: the build others
  download takes the owner's own API key, and his own build keeps his
  subscription ("如果我大包包装的那一个只允许apikey 但是我自己继续用这一版应该没有关系吧").
- Claude Code takes cloud-provider settings (`CLAUDE_CODE_USE_BEDROCK`,
  `CLAUDE_CODE_USE_VERTEX`) first, then `ANTHROPIC_AUTH_TOKEN`, then
  `ANTHROPIC_API_KEY`, and the `/login` subscription last
  (https://code.claude.com/docs/en/authentication, "Authentication
  precedence"). A `claude` started with none of them in its environment runs
  on whatever subscription the Mac is signed in with.
- The terminals the window opens (`/term`, a session handed to the terminal)
  copy the host's own environment, and the owner's own `claude` there is
  their own use of Claude Code.
- The daemon loads every name in its Keychain item into its own environment
  at start (`load_env_file`); its Anthropic client reads `ANTHROPIC_API_KEY`
  from there (`jarvis/decision/llm.py`), and first run rewrites that item from
  the daemon process (`save_key` reads, merges and writes it).

## Decision

In the installed app, every `claude` the agent host starts for a Startrail
session gets the owner's own Anthropic API key, or their Amazon Bedrock or
Google Vertex settings, in that process's environment and nowhere else;
without one, the host starts no Claude session and says what is missing. The
dev build keeps Allen's subscription.

Limits:

- A key is checked by listing models, which costs nothing, before it is kept.
  It lives in the login Keychain in an item of its own (service `Jarvis`,
  account the agents folder), which the daemon never loads and erasing all
  data deletes.
- Claude Code's own sign-in on the Mac is never read, changed or restricted:
  the host's environment, the terminals it opens and `claude update` get no
  key.
- The installed app keeps the window off, and ships no Claude Code of its
  own, until Allen confirms he has agreed to Anthropic's Commercial Terms,
  which running Claude Code in a product requires.
- Codex is unchanged: the owner's own Codex sign-in, started through Codex's
  own login flow.

## Alternatives rejected

- **Put the key where every `claude` reads it** (the host's environment, or
  Claude Code's settings) — the owner's terminal `claude`, `/term` and the
  sessions handed to a terminal would move from their subscription to API
  billing without a word, which restricts a sign-in method built into Claude
  Code; the legal page forbids that to a product running it.
- **Run Startrail's sessions on the subscription the Mac is signed in with**
  — that routes an Agent SDK product's requests through the owner's Pro or
  Max plan, which the legal page does not permit without Anthropic's prior
  approval, and none has been asked for.
- **Keep the key in the daemon's Keychain item** — the daemon would load it
  as `ANTHROPIC_API_KEY` into its own process, where its Anthropic client
  and every child that does not strip it would use it, and first run and the
  host would read, merge and rewrite the same item, so one can drop the
  other's key.

## Consequences

- An owner who pays only for a Claude subscription gets no Claude sessions in
  the installed app's Startrail until they add an API key or a cloud account;
  every token there is billed by the API, and Jarvis has no spend cap yet
  (改15).
- One conversation can be billed two ways: a session handed to the terminal
  goes on there on the terminal's own sign-in.
- The host reads the login Keychain when it starts; a locked Keychain shows as
  the reason Claude sessions cannot start.
- Microsoft Foundry, the third provider the legal page names, is not offered.
