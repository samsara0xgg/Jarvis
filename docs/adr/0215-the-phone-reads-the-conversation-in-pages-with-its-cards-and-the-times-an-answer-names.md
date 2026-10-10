# ADR 0215 — The phone reads the conversation in pages, with its cards and the times an answer names

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- The conversation route answered the newest page or the rows past a cursor. To show older days
  the desktop window asked again for a longer newest page each time, so each scroll fetched every
  row it already held, and `limit` had no cap.
- The prompt's history starts at `session.history_since`; memory.db holds the records before it,
  and she can search them. Whether the phone's scroll-back should stop at the same row was left to
  Allen, who chose that it should only bound the page he opens on.
- A phone's row stream carried only the answer's rows. The bus card and the ask card reached the
  Mac's own surfaces, so a phone talking to her learned of a card only when it next looked.
- The phone turns a dial to the time an answer is about. The answer's words say "明天早上" with no
  zone; the turn's own log holds the exact instant for each thing it did.
- The final `surface.response_emitted` row already carries the spoken and the written part, and
  the phone reads its written part from there.

## Decision

Page the conversation back from a cursor, give the phone its turn's cards in its row stream, and
put the times a turn set or showed on its final answer row, read from that turn's own log.

Its limits:

- **Pages go back by `seq`.** `before` returns the newest rows older than it, oldest first. A page
  holds at most 500 rows and at most 512 KB of text, and always at least one row. Asking for
  `after` and `before` together is refused (400).
- **The floor bounds only the page he opens on.** The newest page and the rows past `after` start
  at `history_since`; `before` goes back to the first record. `has_more` says older rows exist,
  including those below the floor.
- **Cards ride the row stream.** `clarification.requested` and `clarification.withdrawn` of the
  device's own turns are sent with its answer rows, and are never read by its speech. The phone
  reads the slot's current card from the route that already exists, with the device token every
  route already admits.
- **Times come from what the turn did.** Reminders it scheduled, Outlook events it created or
  updated without an error, and the first leave time of its transit card: at most five, in the
  order they ran. A zone name that is not an IANA name is skipped. Failing to read them never
  costs the answer.

## Alternatives rejected

- **A larger `limit` on the newest page.** The work per scroll grows with the history: with 5,000
  rows, each scroll re-reads and re-sends 5,000. A cursor sends only the page.
- **Offset paging.** Rows arrive while he scrolls, and an offset then names different rows. The
  `seq` of the oldest row he holds does not move.
- **Stop `before` at the floor.** His scroll-back would end where the model's memory ends and hide
  records that exist and that she can search. Allen chose otherwise.
- **Read the times from the answer's text.** Words like "明天早上" carry no instant, and a dial
  turned to a guess is worse than no turn. The log holds the epoch milliseconds.
- **Send cards only through a poll of the slot.** A card withdrawn between two polls is never seen,
  and the row stream already delivers the turn's rows in log order.

## Consequences

- The desktop window's "older" now crosses the floor too, and shows records from before the
  current window.
- `has_more` costs one indexed existence query per page.
- An event waiting on its confirmation card has no result yet, so it gives no time on that turn's
  answer. An update that moves no time gives none.
- Card rows add the transit card's options to the phone's stream, a few hundred bytes a card.
