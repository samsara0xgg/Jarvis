# ADR 0051 — The home reads Microsoft without a model, and a click checks a to-do off

**Status:** Superseded-by-0055
**Date:** 2026-09-25
**Supersedes:** 0036

## Context

- Everything ADR 0036 weighed still holds: Allen's todos live only in
  Microsoft To Do and his schedule only in Outlook calendar, on a personal
  account reached through the community `ms-365-mcp-server` (`microsoft`);
  the daily report reads them itself through read-only tools outside the
  model's turn, and there is no Pre-action Gate on that path.
- Allen decided the companion home on 2026-09-25: a fixed Today block with
  the weather, the rest of today's calendar and checkable to-dos, and a
  mail block for unread mail from people. The panel polls these while open;
  a model call per poll would cost money and seconds for data that needs no
  judgment. The live reads take about 1.2 s for calendar, lists and tasks.
- ADR 0036 kept every write to Microsoft model-proposed and confirmed per
  ADR 0033. That confirmation exists because a model's proposal may not be
  what Allen wants; a checkbox click is Allen's own act on one named task.
- Allen's personal mail is Gmail; he does not use the Outlook mailbox
  (2026-09-25). Google's hosted Gmail MCP server refuses @gmail.com
  accounts (checked 2026-09-23). Gmail serves IMAP to an app password and
  accepts Gmail search terms through IMAP's `X-GM-RAW`, including its own
  Primary category. Python's standard library speaks IMAP.
- The Microsoft server's scopes follow its enabled tools; a new mail tool
  would ask for a scope the saved login lacks and stop every tool of the
  server, calendar and To Do included, until a fresh login.
- Open-Meteo serves a forecast for a latitude and longitude with no key.

## Decision

Keep Allen's todos and calendar only in Microsoft To Do and Outlook calendar
and let the daily report read them as ADR 0036 decided; the companion home
also reads today's calendar and open tasks through the `microsoft` server's
read-only tools outside any model turn, and the one write outside a model
turn is a task's completed or not-started status sent when Allen clicks its
checkbox on the home. Every other write to Microsoft stays model-proposed and
confirmed per ADR 0033. The home reads unread Primary mail from Gmail over
read-only IMAP with an app password, and its weather from Open-Meteo for the
place set in config.

## Alternatives rejected

- **Ask the model for Today** — each poll would be a paid call of several
  seconds to copy three lists the tools already return as JSON, and the
  times and titles would be the model's copy instead of Graph's.
- **Route the checkbox through a model turn and its confirmation** — the
  click already names the task and the change; a second question about the
  same click is the confirmation asking itself.
- **Outlook mail through the `microsoft` server** — Allen's mail is not
  there, and its scope change would take calendar and To Do down until a
  new login.
- **Gmail API or a Gmail MCP server** — both need Allen's own Google Cloud
  project and OAuth client for a personal account; IMAP needs one app
  password and no dependency.
- **Mail filtered by sender rules only** — Gmail already sorts each message
  into Primary or another category for this account; our own list of
  newsletter senders would lag it. A short no-reply pattern only trims what
  Primary lets through.
- **Weather from the Mac's location service** — it needs a location
  permission prompt and a native helper; a configured place is one line.

## Consequences

The home and the report share one community server and one token file. A
checkbox write has no undo in Jarvis beyond clicking again, and it is not
recorded in the event log. The Gmail app password sits in plain text in the
runtime env like the other keys, gives full mailbox access, and stops working
if Allen turns off 2-Step Verification. The model gets no mail tool. The
weather is for the configured place, not where the Mac is, and a clear night
shows the sun icon because the home has no night icons. The brief block
shows yesterday's saved report as it was generated; a report generated
during that day still says so.
