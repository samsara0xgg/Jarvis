# ADR 0063 — Jarvis reads and replies to Gmail in the conversation

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- Allen, 2026-09-26, asked for a mail flow:
  1. He asks Jarvis to find an email, then asks how to answer it.
  2. Jarvis shows him the email itself first.
  3. When he says reply, Jarvis shows the drafted reply as a card for him
     to approve.
- He also ruled out writing his Gmail address into the profile. A public
  build serves other people's accounts, so the address must come from the
  Gmail account the user connected, and only after it is connected.
- The conversation page draws memory.db `records`. The model's history is
  built from the same rows, and they carry words only, without tool results
  (ADR 0044). So an email the model read in one turn is gone by the next
  turn, and a reply needs that email's `threadId`.
- Google's Workspace server (ADR 0055) behaves like this:
  - `gmail_get` returns a message as JSON text: id, threadId, from, to,
    date, subject, and the body as text or raw HTML.
  - `gmail_send` has no thread field.
  - `gmail_createDraft` takes a `threadId` and sets In-Reply-To and
    References, and `gmail_sendDraft` sends it.
  - No enabled tool returns the account's own address. `people.getMe` is
    switched off, and the only scope is `gmail.modify`.
  - A failure comes back as `{"error": ...}` in an ordinary text block, not
    flagged as an error. So a failed send read as a success.
- Jarvis runs a manual clone of that server; changing it would be a fork
  that a public install does not have.

## Decision

Jarvis adapts the Gmail server's tools on its own side. It does not fork
the server, and it keeps nothing about the user in the profile.

- **Showing an email.** A turn that read exactly one message whole with
  `gmail_get` writes it as a `mail` record: headers (From, To, Date,
  Subject, Thread-Id, Gmail-Id), a blank line, then the body as plain text,
  capped at 4000 characters. The record goes in ahead of the answer, so the
  page shows the email above the answer, and the next turns' history still
  has it.
- **Replying.** `gmail_send` gains an optional `threadId`. With it, the one
  confirmed call drafts the reply into that thread and sends the draft.
  `gmail_createDraft` stays off the model's menu: the waiting card is the
  draft (ADR 0062), and on 2026-09-26 the model, given both tools, answered
  "回复吧" with `gmail_createDraft`, whose confirmation saves and never
  sends.
- **The user's address.** On connect, Jarvis reads the address off the
  newest sent message and writes it into `gmail_send`'s description.
- **Failures.** A text block that is exactly `{"error": ...}` is a tool
  error.

## Alternatives rejected

- **Put the address in the profile** — Allen refused it on 2026-09-26: a
  public build would carry one person's address, and it would be there even
  when no Gmail is connected.
- **Add `getProfile` and a threaded send to the server's source** — Jarvis
  runs a manual clone of Google's repository (ADR 0055). A patched clone is
  a fork that every other install lacks, and it is lost on the next pull.
- **Let the model call `gmail_createDraft` then `gmail_sendDraft`** — both
  carry no read-only hint, so each asks (ADR 0033). The second ask shows
  only a draft id, so Allen would approve a letter he cannot see.
- **Show every fetched message as a card** — a turn that looks through
  several messages to answer "anything new?" would stack cards that nobody
  asked to read. One message read whole is the "show me that email" case.

## Consequences

- An email longer than the tool result cap (8192 characters, windowed) is
  not valid JSON by the time it is recorded, so it shows no card.
- HTML-only mail loses its layout, and quoted history stays in the body.
- An account with an empty Sent folder, or a send-as alias, gives no
  address or the wrong one. The model then has to be told the address.
- The address is read once per connection. Switching accounts needs a
  daemon restart.
