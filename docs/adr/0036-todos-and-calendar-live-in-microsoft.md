# ADR 0036 — Todos and calendar live in Microsoft; the daily report reads them

**Status:** Accepted
**Date:** 2026-09-23
**Supersedes:** 0020

## Context

- Allen, 2026-09-23: Jarvis connects to third-party apps instead of rebuilding
  them; his todos go to Microsoft To Do and every schedule (interviews, info
  sessions, co-op applications) to Outlook calendar with Teams meetings, on a
  personal Microsoft account. The daily loop Jarvis owns is turning TimeSink,
  Git and conversation evidence into a plan and progress tracking.
- ADR 0020 kept todos, sourced knowledge and saved briefings as local
  event-derived records and refused Microsoft sync because two copies of one
  todo need a sync protocol. No `todo.revised` event was ever written
  (count 0 on 2026-09-23).
- Microsoft's hosted MCP servers (Work IQ, Agent 365) need a work or school
  account and a Microsoft 365 Copilot licence and have no To Do server. The
  community `ms-365-mcp-server` serves To Do and calendar to a personal
  account over Graph as a local stdio process, and marks every GET tool
  readOnlyHint.
- The daily report is composed by code from one gathered day (ADR 0028). No
  code calls an MCP tool outside a model turn, and the report service is
  built before MCP servers connect.
- Graph's To Do task listing answers `$select` with 400
  (RequestBroker--ParseUri); calendarView answers in UTC; To Do due and
  completion values are dates at midnight, not instants.

## Decision

Keep Allen's todos and calendar only in Microsoft To Do and Outlook calendar,
reached through the configured `microsoft` MCP server; the daily report reads
the report day's and the next day's events and every open or same-day
completed task itself, through read-only tools outside the model's turn, and
code writes the next day's schedule and open tasks into the saved report.
The local todo tools leave the model's menu; sourced knowledge and saved
briefings stay local event-derived records as ADR 0020 decided. Writes to
Microsoft stay model-proposed and confirmed per ADR 0033.

## Alternatives rejected

- **Keep local todos and sync them to To Do** — completing one copy still
  leaves the other open without a sync protocol, and with zero local todo
  events there is nothing to migrate.
- **Let the report model call the Microsoft tools** — the draft already has
  at most three query rounds for checking the day (ADR 0028); calendar and
  task calls would spend them, and every time and title in the next-day
  section would be copied by the model instead of written from one read.
- **Notion as the task store** — its server was wired first (ADR 0035), but
  Allen's interviews and meetings live in Outlook calendar and Teams, and one
  vendor for tasks and calendar is one login and one read.
- **Microsoft's hosted Work IQ servers** — they refuse personal accounts and
  have no To Do server.

## Consequences

The report now depends on a community server pinned by version, its token
file in `~/Library/Application Support/ms-365-mcp-server/` and Microsoft's
availability; a failed read is written into the report and its coverage and
is not retried. The background read has no Pre-action Gate in front of it, so
only read-only tools may be called that way. The next-day section is as of
generation: a report regenerated later shows the plan at that time. A day
with calendar entries but no recorded activity is still `no_evidence` and
gets no report. The local todo store, its schemas and `todo.revised` stay as
unreachable code until removed, and work state reads no Microsoft tasks yet.
