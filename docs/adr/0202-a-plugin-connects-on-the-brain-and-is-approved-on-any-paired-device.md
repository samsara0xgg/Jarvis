# ADR 0202 — A plugin connects on the brain and is approved on any paired device

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** 0038

## Context

- ADR 0038 made a plugin connection a workflow a conversation requests and an
  authenticated desktop action starts: she opens the panel, Allen clicks
  Connect, the daemon opens a browser on its own machine, and the OAuth
  redirect lands on a loopback listener there (`127.0.0.1:8789/callback`).
- With the brain on a Raspberry Pi (ADR 0170) none of that reaches Allen. The
  Pi has no browser, a loopback redirect lands on whichever device approved,
  and the plugin routes demand the brain's local key, which no paired device
  holds (ADR 0183 left them "until a later decision gives them one").
- Allen wants to connect, turn off and remove plugins by saying so, and to
  approve the login on whichever device is in his hand, the phone included.
- Probed on 2026-10-09 with dynamic client registration: 16 of the 17 hosted
  MCP servers that offer it (Notion, Linear, Atlassian, Stripe, Sentry and
  others) accept an `https` redirect on the brain's Tailscale name; most
  refuse plain `http` off loopback. Vercel accepts loopback only. Slack, Zoom
  and Shopify offer no registration; Figma refuses unlisted clients.
- The authorization URL carries no secret: the PKCE verifier and the tokens
  stay on the brain.

## Decision

A plugin connects on the brain. Any paired device manages it, and the login is
approved on whichever device opens the link.

- **Who may manage.** The plugin routes, and the language, Codex reset and
  balance routes, accept a paired device's token as well as the local key.
  Every paired device is Allen's own (ADR 0196).
- **She starts it.** "Connect Notion" opens the request and starts the
  connection at once when the plugin needs no typed credential. A plugin that
  needs a typed key still waits for its field on a page.
- **The brain opens nothing.** The authorization URL is part of the request's
  state, readable by the devices that may manage plugins. The UI that shows the
  request opens the link on its own device; any other device may open it too.
  The first approval wins, and the request then reads `ready` everywhere.
- **The redirect is configurable.** `tools.mcp.oauth_redirect_uri` is
  registered in place of the loopback URI. On the Pi it names
  `https://<brain>.ts.net/oauth/callback`, which `tailscale serve` forwards to
  the unchanged loopback listener. Unset, the loopback URI stays, so a daemon
  on the Mac behaves as before. A stored registration for another redirect is
  dropped and registered again at the next login; its tokens keep refreshing
  until then.
- **Off and removed by voice.** "Turn Notion off" disables it as the panel's
  button does: tools leave, the login stays. "Remove Notion" disables it and
  deletes its stored login and credentials on the brain, after Allen confirms
  on a card (ADR 0062). Revoking access at the service stays his.

## Alternatives rejected

- **Find the device Allen is using and send the link only there** — nothing
  on the brain knows which device is in his hand (phone presence is a batched
  report, ADR 0197); a wrong guess leaves the link where he is not. Every
  device can show it, and only one approval completes.
- **Keep the loopback redirect and relay it through the Mac** — works only
  where a terminal runs; a phone has no listener, so the phone approval Allen
  asked for is impossible.
- **Open the whole brain through `tailscale serve`** — ADR 0183 rejected a
  generic proxy for the UI; one callback path exposes nothing else.
- **Plain `http` on the Tailscale name** — 12 of the 17 servers refused it at
  registration.
- **A device-code flow for every server** — the hosted servers offer
  authorization-code with PKCE only; the SDK ships no device-code poller (ADR
  0032).

## Consequences

- A paired device's token now manages plugins and their credentials;
  unpairing a lost device (ADR 0196) is what revokes that.
- Logins approved before this decision keep working; the first new login per
  server re-registers it with the new redirect.
- Vercel, Slack, Zoom, Shopify and Figma cannot be connected on a Pi brain.
- Servers that log in through their own command (Microsoft's device code,
  Gmail) still log in with `mcp-login` on the brain's machine.
- A phone shows the request only when its app reads it; there is no push.
- The callback path answers only while a login is pending; at other times the
  Tailscale path returns an error page.
