# ADR 0354 — Device and plugin-panel tools wait behind search, every search is an event, and a plugin tool is marked by what it does

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0247, keeping the plugin panel's four tools on the menu; in ADR 0287, a search leaving a trace only in the LLM io log; in ADR 0320, counting only the tools loaded into the request as on the menu when an offered lookup is checked

## Context

- The resident menu goes out with every request: about 38 tools and 38,000 characters by the audit
  of 2026-10-20. `docs/spec.html#tools` puts behind search (deferred) the tools that are rare, or
  useful only while a device or a plugin is connected.
- Two groups break that rule:
  - **The device tools** (ADR 0225): `open_path`, `read_file`, `read_clipboard`, `search_notes`,
    `open_url`, `screen_look`, `start_night_run` and `end_night_run`. On a brain they run only on a
    connected terminal, and their descriptions are about 4,000 characters of the menu.
  - **The plugin panel's four** (`list_plugins`, `open_plugin`, `disable_plugin`,
    `remove_plugin`): 2,317 characters, about 600 tokens per request, for 11 uses by that audit.
    ADR 0247 kept them on the menu because a deferred tool then cost a client `tool_search` round.
    Since ADR 0287 the search is Anthropic's own, inside the same response. On 2026-10-16 Allen
    decided to put the panel behind search; nothing recorded it.
- Deferred tools stay in the request's tool list, marked for search, so deferring a tool does not
  change the cached prefix from turn to turn (ADR 0287).
- Marks that say the wrong thing:
  - `open_plugin` starts a connection and can open a login, yet it is marked read-only (L0).
  - `list_plugins` may return 24,000 characters; `docs/spec.html#tools` keeps a result to about
    4,000.
  - Hue's six reads carry no read-only hint. The manifest approves them, so they run without a
    card, but a receipt counts each as a change (ADR 0319). A listing can then back a claim that
    the lights were turned off, and a true "I looked" citing it is sent back (ADR 0320).
- ADR 0320 sends back an offer to look something up when a read-only tool on the menu could do it.
  It counts as on the menu only the tools loaded into the request, so a deferred read is never
  caught.
- The system prompt carries four lines on how to connect, turn off and remove an app, naming the
  panel's tools. On 2026-10-21 Allen said a prompt should hold only the task and the format, and
  that removing existing rules is in that spirit, tested on its own.
- A native search shows only in the LLM io log (ADR 0287), so a search that misses is invisible,
  and whether a plugin tool can be found has never been examined.
- On 2026-10-21 Allen approved the plan for these tools (「就按你说的去吧」): put the panel behind
  search, mark it truly, record it in an ADR; log every search as an event; put plugin tools in the
  exam; an absent device's tools should not fill the menu.

## Decision

Put the device tools and the plugin panel behind search, log every search, and mark every plugin
tool by what it does.

- **Behind search.** Every device tool and the plugin panel's four are deferred wherever they are
  offered. Tier 0 still calls its device lines directly. An offered lookup that a deferred
  read-only tool could do is sent back like one a loaded tool could do.
- **True marks.** `open_plugin` is an action that changes something, not a read. `list_plugins`
  returns at most about 4,000 characters: each plugin's id, name, state and one line of what it
  does. Where a plugin's server leaves its reads unmarked, its manifest marks them read-only, Hue's
  six first.
- **No prompt lines for plugins.** The four lines go from the system prompt. What each panel tool
  does and returns moves into its own description.
- **Every search is an event.** Each native search is written to the event log: what was searched
  for, and the tools that came back, or none.
- **Plugins in the exam.** The exam lists plugin tools through their manifests and recorded answers
  (ADR 0317). Its whole-turn items include finding a deferred tool by search and connecting an app.

## Alternatives rejected

- **Keep the device tools resident, so she never has to search for them.** Since ADR 0287 a search
  adds no request; staying resident costs about 4,000 characters on every request, used or not.
- **Drop a device's tools from the list while it is away.** ADR 0225 rejected it: each connect and
  disconnect would change the head of the cached prompt.
- **Count a plugin's listing as a change, as now.** A claim that the lights are off would pass on the
  receipt of a listing (ADR 0320), which is the 2026-10-17 failure in a new form.

## Consequences

- A deferred tool is found by a search before its first call in a turn. A search that finds the
  wrong tool is now visible, not prevented.
- A wrong read-only mark in a manifest hides a change from the receipts. Manifests are reviewed as
  code.
- Without the prompt lines, how she connects an app rests on the panel tools' descriptions and on
  the exam.
- Each search adds one event to the log.
