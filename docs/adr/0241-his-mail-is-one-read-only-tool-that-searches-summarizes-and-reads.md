# ADR 0241 — His mail is one read-only tool that searches, summarizes and reads

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** ADR 0190, and the clause of ADR 0127 that puts `gmail_search` and `gmail_get` in the shipped `always_loaded` list. Everything else in ADR 0127 stands.

## Context

- The model had three mail reads: `mail_inbox` (ADR 0190, the unread overview with Jev's marks),
  and the Workspace server's `gmail_search` and `gmail_get`, on the menu from the first request
  (ADR 0127, `tools.mcp.always_loaded`).
- `gmail_search` returns message ids only, so every result costs a `gmail_get` to read. Live data
  on the Pi brain, 2026-10-11: `gmail_get` was called 270 times, one turn made 96 of them.
- The replay exam (`scripts/tool_exam.py`) shows the same shape. "Find all mails about co-op"
  loops `gmail_search` and `gmail_get`; "open that email" calls `tool_search` six times for
  tools that were already on the menu.
- A whole-message read still has to draw the mail card (ADR 0063) and give a reply what it needs
  (the sender's address, the thread id). Job mail (ADR 0155) reads Gmail on its own path and
  is not a model tool.

## Decision

The model's mail is one read-only built-in tool, `mail`, on the menu from the first request:
no arguments give the unread overview `mail_inbox` gave, `query` searches and returns up to 8
messages with sender, subject, local time and snippet (headers read in parallel by the server,
the next page held by the server and asked for with `more`), and `read` opens one message as
clean text of at most 4000 characters. `mail_inbox` goes; `gmail_search` and `gmail_get` leave
`always_loaded` and stay deferred behind `tool_search`, and the tool reads Gmail only through
those two calls.

## Alternatives rejected

- **Keep the three tools and rewrite their descriptions.** The 96-call turn ran with both Gmail
  tools loaded and described; ids-only search makes N results cost N sequential calls whatever
  the text says, and the overview stays a third entry the model must choose between.
- **Wrap the Gmail tools so `gmail_search` returns headers.** It keeps `gmail_get`'s one-call-
  per-message habit and the separate overview, and the exam's two failing shapes stay: the loop
  and the redundant `tool_search`.
- **Separate `mail_search`, `mail_read` and the overview.** Three entries again, each with its
  own schema on every request, and the model chooses between them before it has seen a result;
  one tool moves that choice into an argument and takes two entries off the menu.

## Consequences

- A search is one Gmail search plus up to eight parallel metadata reads instead of one call;
  Gmail's total is only an estimate and is named only when it exceeds what has been shown.
- The held page belongs to the last search in the daemon's process: another query, or a restart,
  ends `more`.
- Trimming quoted replies and signatures looks for English markers only, and a trimmed message
  cannot show the text it quoted; a forwarded letter is never trimmed.
- A `tool_search` hit on the Gmail server still loads `gmail_search` and `gmail_get` for that
  turn (ADR 0056), and a settings file that sets `always_loaded` itself keeps them on the menu.
- The mail card reads the new tool's result as well as `gmail_get`'s.
