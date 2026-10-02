# ADR 0115 — The companion says which tool is running

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- Allen, 2026-10-01: while a voice turn waits on a tool the companion shows
  only "Thinking", so a long wait looks like a hang.
- Tool run times on his log (2026-09-26 to 09-29): under 0.6 s for
  `tool_search`, `search_records`, `get_current_time`, the Gmail, calendar,
  Notion and Hue reads; `web_search` 1.9 s; `screen_look` 3.3 s;
  `refresh_work_state` 7.6 s median; `daily_work_report` 58 s.
- The companion cannot see actions: the Inherent v1 socket carries answer
  text and voice phases only, and `action.running` names no tool (the name is
  on `action.proposed`).
- ADR 0008 D6 forbids a model call or a timer only to produce a progress
  line, and text shown for a tool that returns in under a second is a flash
  worse than none.

## Decision

While a tool is running, the daemon sends the surface one fixed line of the
language table for that kind of work, and the surface shows it where it
shows the state. The line is driven by the action rows alone: it appears when
`action.running` is committed and goes when the action reaches a terminal
row or its turn ends; with two tools running, the latest shows.

Its limits:

- A tool on the slow list (`web_search`, `web_fetch`, `screen_look`,
  `refresh_work_state`, `daily_work_report`) shows at once. Any other tool
  shows only if still running after 1.5 s, with its own line for a Gmail,
  Microsoft (calendar) or Notion read and "Working on it..." otherwise.
- The text is chosen by tool name and written in the daemon's language; no
  model writes it and nothing about the tool's arguments or result is sent.
- It shows only for the turn the surface is waiting on, and ends when the
  answer opens. Until then the surface keeps a tool's line through the
  daemon's clearing until the next tool's line replaces it (2026-10-02,
  Allen: each tool's line holds until the next one).

## Alternatives rejected

- **Show a line for every tool on `action.running`.** Most tools on the log
  return in under 0.6 s, so most tool turns would flash text for a fraction
  of a second.
- **Let the model write the status line.** It costs a call and adds latency
  to the very wait it describes, and ADR 0008 D6 already rules it out.
- **Send the tool name and let the surface word it.** The surface language
  and the daemon language are separate settings, and the daemon's table is
  already the one place its fixed sentences live in both languages.

## Consequences

- A tool not on the list and under 1.5 s is never announced, so a short
  tool that precedes a slow model call still reads "Thinking".
- The line is in the daemon's language, which can differ from the
  companion's own interface language.
- A surface that connects while a tool is already running shows nothing
  until the next change.
