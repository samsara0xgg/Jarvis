# ADR 0055 — Gmail comes through Google's Workspace MCP server

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** 0051

## Context

- Everything ADR 0051 weighed for Microsoft still holds: todos and calendar
  live in Microsoft To Do and Outlook calendar behind the `microsoft` server;
  the home polls Today, the mail and the brief without a model turn; a
  checkbox click is Allen's own act on one named task.
- 0051 read Gmail over IMAP with an app password. Allen never made one, and
  the model got no mail tool: on 2026-09-26 he asked Jarvis by voice for his
  latest mail, and it could only answer that no mailbox was connected.
- The Codex route was tried and dropped the same day: Codex's Gmail is the
  ChatGPT connector of whatever account Codex is logged into, and on this Mac
  that is another person's account and mailbox. Allen: "不是好方案".
- Google publishes a Workspace MCP server for Gemini CLI
  (`gemini-cli-extensions/workspace`, Apache-2.0, a stdio Node process). It
  signs in with Google's own OAuth client; the code exchange and every
  refresh go through Google's cloud function, which holds the client secret,
  so the user needs no Google Cloud project. Mail itself moves only between
  the Mac and Google. With every other feature group switched off it asks for
  `gmail.modify` alone and lists search, get and listLabels as read-only and
  eleven more tools (send, drafts, labels, attachment download) without
  hints. It answers failures as `{"error": ...}` in a normal text block, and
  opens a browser for a missing login unless the environment says CI.
- Alternatives measured the same week: Google's hosted Gmail MCP is a
  Workspace developer preview barred from public apps; a self-built Google
  Cloud client works but costs a project, a consent screen and a Production
  switch against 7-day tokens; Composio's hosted connector puts mail and
  Google tokens on its servers and exposes 7 meta-tools, so reads and sends
  share one tool.
- The daemon must never open a browser (ADR 0032); a server that keeps its
  own login needs the same environment at login as in the daemon, or it asks
  for other scopes and later deletes the token as short of them.

## Decision

Run Google's Workspace MCP server as the `gmail` MCP server with only Gmail
switched on: the model reaches its tools like any MCP tool (reads unasked,
every other tool confirmed per ADR 0033), and the companion home reads unread
Primary mail through its read-only search and get outside any model turn, as
it reads Today through `microsoft`'s read-only tools; the one write outside a
model turn stays the to-do checkbox of ADR 0051, and every other write to
Microsoft or Gmail stays model-proposed and confirmed. A local server's own
login runs from `mcp-login` with the entry's command and environment.

## Alternatives rejected

- **Keep IMAP with an app password** — the model still gets no mail tool,
  and Allen still has to make a password that grants the whole mailbox; the
  server needs one browser consent and no secret in the env file.
- **Codex's Gmail through `codex app-server`** — it reads the mailbox linked
  to the ChatGPT account Codex is logged into, which on this Mac is not
  Allen's (checked 2026-09-26 in the tools' `link_owner_profile`).
- **Composio Connect** — mail and the Google tokens sit on Composio's
  servers, and its single execute tool would make Jarvis confirm every read
  or no send.
- **Allen's own Google Cloud client with a community server** — the same
  tools after a project, a consent screen and a Production switch; nothing it
  adds is needed now.
- **Let the server open the browser from the daemon** — a home poll without
  a login would open a tab every few seconds; CI=1 turns that into an error
  the home shows and the model can relay.

## Consequences

Gmail now depends on Google keeping that cloud function and client for
Gemini CLI; if either goes, the entry can point the server at our own client
through its environment. The refresh token passes through Google's function
on every refresh. The install is a manual clone and build outside this
repository, and `$HOME` in the entry assumes the same place on every Mac.
`gmail.modify` lets a confirmed tool send, archive or delete. Without a login
the home's mail shows the 502 path, not the 404 "not connected" one. The
model's mail tools sit behind `tool_search` and are found only when a turn
takes the tool path.
