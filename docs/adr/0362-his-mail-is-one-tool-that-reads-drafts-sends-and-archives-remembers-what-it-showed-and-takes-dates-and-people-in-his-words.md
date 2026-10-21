# ADR 0362 — His mail is one tool that reads, drafts, sends and archives, remembers what it showed, and takes dates and people in his words

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0242, `mail` as a read-only tool whose search returns snippets and whose read opens one message; in ADR 0063, `gmail_send` as the model's way to send

## Context

- His mail is four tools today:
  - `mail` reads only: the unread overview, a search, one message (ADR 0242);
  - `write_mail_draft` drafts only under the letter open on the Mac's Dashboard (ADR 0148);
  - the plugin's `gmail_send`, wrapped to reply in a thread (ADR 0063);
  - the plugin's other Gmail tools, behind tool search.
  A reply takes three to four calls.
- `mail`'s description steers away from reading: a search is "usually enough to answer", and a
  read is "only when he asks what it says". A read needs a search first for the id.
- Search results do not reach the next turn; only receipts do (ADR 0319). After a search,
  「第二封说什么」 has no id to read.
- What went wrong:
  - 2026-10-16: she checked a news item from his mail against the web without opening the mail.
  - 2026-10-16: asked how much a card statement wanted, she gave the date and no amount.
  - 2026-10-18, Cloudflare: over four turns she said paid, then not paid, then asked him to look
    himself. Each turn answered from a new search's snippets, and no body was read.
- She writes Gmail's query language herself:
  - dates as `newer_than:`;
  - on 10-16 her query dropped the sender "wealthsimple".
- The spec's tool standard (`docs/spec.html#tools`) asks for:
  - times and people taken in his words and resolved by the tool;
  - result sets remembered by the server;
  - one need done in one call;
  - a description that says when not to use the tool.
- The Gmail server is Google's Workspace MCP server (ADR 0055). It gives:
  - search;
  - a message in its own text form or as the raw message;
  - attachment download and label changes;
  - send, create a draft, send a draft.
  It has no thread read, and no draft update or delete.
- On 2026-10-21 Allen approved the plan's fix (「就按你说的去吧」):
  - one mail tool reads and writes;
  - the server remembers the last results, so 「第 2 封」 works across turns;
  - a single hit comes with its body.

## Decision

Make his mail one tool, `mail`. It lists, searches, reads, opens a thread or an attachment,
drafts, sends and archives; it remembers what it last showed; and it takes dates and people in
his words.

- **One tool, by action.**
  - The actions: list (the default), search, read, thread, attachment, draft, send, archive.
  - Effects (ADR 0361): the reads are L0; a draft or an archive is L1 with an undo; a send is L3.
  - While `mail` is on the menu, tool search loads none of the plugin's Gmail tools, and says to
    use `mail` (ADR 0242's rule).
  - `write_mail_draft` goes.
- **Remembered.**
  - Each listing numbers its rows. A row's number names that row of the last listing, across
    turns, until the next listing or a restart. A message id always works.
  - A read can take up to three rows at once.
- **The body comes with it.** A search with one match returns that message read whole (ADR 0363).
  Several matches return rows, and each row carries the message's own opening line, not Gmail's
  snippet.
- **His words.**
  - `when` takes his words (today, this week, since Monday, 10月16号). The one day resolver of
    ADR 0356 turns them into exact bounds in his zone.
  - `from` and `to` take a name or an address. Gmail syntax still works in `query`.
- **To the standard.**
  - The description says:
    - use it for any question about his mail: what came, what a mail says, a reply, a new
      letter;
    - his applications are `jobs`'s, and his schedule is `schedule`'s;
    - what it returns.
  - Results stay within about 4,000 characters, cut by whole rows.

## Alternatives rejected

- **Keep reads and writes in separate tools.** A reply took three to four calls across four
  tools. The standard asks for one tool per thing, with reads and writes in it.
- **Return the body of every search row.**
  - Eight bodies of up to 4,000 characters each would pass the result limit eight times over.
  - Rows with their own opening lines, plus reading three of them in one call, stay within it.
- **Put every row's id into the conversation history instead.** Receipts carry one line per call
  (ADR 0319). Listing every row's id there costs tokens on every later turn. The tool's memory
  costs nothing until it is used.

## Consequences

- A row's number refers to the last listing. After a restart she searches again.
- Reading the mails costs a few seconds more than answering from a snippet did.
- The Mac Dashboard's mail page loses `write_mail_draft`. Its draft is the Gmail draft
  (ADR 0366).
