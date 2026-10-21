# ADR 0366 — A mail draft is a Gmail draft, a person he names is found in his own mail, and the send card shows every recipient with his own marked

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0063, his address written into `gmail_send`'s description and the confirmed `gmail_send` that drafts into a thread and sends; in ADR 0148, the reply draft as an in-memory record that exists only under the letter open on the Dashboard

## Context

- Drafts live in the daemon's memory (ADR 0148), and only under the letter open on the Mac's
  Dashboard. A restart loses them, and from the phone he cannot reply at all.
- Sending goes through the plugin's `gmail_send`, wrapped to reply in a thread: create a draft,
  then send it (ADR 0063). Its description names "the user's own address", read off the newest
  sent message. That address is a UVic alias whenever he last sent from it (ADR 0063 notes this).
- 2026-09-26/27: a letter meant for a friend went to his own UVic address.
  - `to` is whatever the model typed, and nothing resolves a name.
  - The Gmail server's People feature is switched off (`plugins/gmail/.mcp.json`).
- The cards:
  - the Mac's letter card shows a bare address that cannot be edited;
  - the phone's card has no reply mark;
  - both hide Cc and Bcc.
- The Dashboard's Send replies to From, and ignores Reply-To and Cc.
- The Gmail server can create a draft in a thread (it sets the reply headers from the thread's
  last message) and send a draft. It cannot update or delete a draft.
- On 2026-10-21 Allen approved (「就按你说的去吧」):
  - drafting and sending inside the mail tool, as real Gmail drafts;
  - a recipient found by name in his mail;
  - a card that shows every argument, name and address, with 「（你）」 on his own.

## Decision

Write a draft as a Gmail draft, find a person he names in his own mail, and put every recipient
on the send card by name and address, with his own marked.

- **Gmail drafts.**
  - `draft` writes a Gmail draft. It is L1, and discarding it is its undo.
  - The draft shows in Gmail on every device and survives restarts.
  - A revision writes a new draft and discards the one it replaces.
  - The tool records each draft it writes (its ids, its thread and its letter) as an event, so a
    send from any device can name it.
- **A reply.**
  - It goes in the thread of the mail it answers, to that mail's Reply-To, else its From.
  - `reply_all` adds that mail's To and Cc, without his own addresses.
  - The subject is that mail's, with "Re:".
- **A person he names.**
  - The tool looks the name up among the senders and recipients of his mail.
  - One clear match is used, and the result names the last mail with that person.
  - Several matches are put to him (needs_him, up to three). No match is an error that asks him
    for the address.
  - An address he gives is used as given.
- **His own addresses** are the From addresses of his sent mail. None of them is written into any
  description.
- **The card.** The tool completes the send before the card (ADR 0361). The card lists:
  - To, Cc and Bcc, each name with its address, and 「（你）」 or "(you)" after any of his own;
  - the subject and the whole body;
  - for a reply, the thread it answers.
  The Mac's edit in place and the tap send exactly what the card holds (ADR 0062). The phone
  answers the same card.
- **The Dashboard.** The Mac's mail page shows the Gmail draft of the open letter's thread. His
  edits there are revisions, and its Send raises the same card.

## Alternatives rejected

- **Keep drafts in Jarvis and write the Gmail draft only at send.** The phone app has no mail page,
  while a Gmail draft opens in Gmail on any device today. A draft in Jarvis's memory dies with the
  daemon, which restarts at every deploy.
- **Turn on the server's People feature for contacts.**
  - It needs a new Google scope and a new consent.
  - The people he writes to are in his mail, which needs neither.
- **Let the model resolve the name.** That is 09-26: the one address it held was his own.

## Consequences

- The server cannot delete a draft. A replaced draft is moved to Trash where Gmail allows it.
  Where it does not, the older draft stays in Drafts, and the result says so.
- A name that matches several people needs his answer before any card appears.
- The draft events keep each letter's text in the event log.
