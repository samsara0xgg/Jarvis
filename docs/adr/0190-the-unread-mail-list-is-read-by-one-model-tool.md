# ADR 0190 — The unread mail list is read by one model tool

**Status:** Accepted
**Date:** 2026-10-07
**Supersedes:** none

## Context

- Live 2026-10-07: asked whether anything in the inbox needed an answer, the model made about
  40 raw Gmail calls (`gmail_search`, then `gmail_get` on every letter) and took 41 s.
- `Home.mail()` already lists unread Primary mail with each letter's `reply` mark (ADR 0123),
  junk flag (ADR 0124), `importance` and `category` (ADR 0141), cached per message id; only the
  Mail page could read it.
- Native tools are on the model's menu from the first request; plugin tools wait behind
  `tool_search` (ADR 0127, ADR 0169).

## Decision

The registry carries one read-only model tool, `mail_inbox`, with no arguments: it returns
`Home.mail()`'s unread list as compact rows (Gmail id, sender name, subject, received time,
reply mark, importance and category when rated) and the count of junk letters it left out. It
is L0 with no confirmation. Its description sends the model here first for "what needs a
reply" and to `gmail_get` only for one letter's text; Gmail not connected is a tool error.

## Alternatives rejected

- **Teaching the model to search and read fewer letters.** A letter's mark is computed once
  per id by Jev (ADR 0123); raw reads redo that work with no mark, and the 41 s run was a
  search plus a metadata read per letter.
- **A prefetch for Jev's `mail_read` group (ADR 0140).** That group's recall is partial, and a
  prefetch pays one Gmail read per predicted turn; the tool costs a read only when called.

## Consequences

- Each call is a full `Home.mail()` read (one search and a metadata read per hit), which also
  refreshes the junk and listed id sets the Mail page's archive taps are checked against.
