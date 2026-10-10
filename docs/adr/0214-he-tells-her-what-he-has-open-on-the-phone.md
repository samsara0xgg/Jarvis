# ADR 0214 — He tells her what he has open on the phone

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- On the phone he has one item open, a reminder, an Outlook event, a task or an entry of the day
  line, and says "把它推到明天". The words name nothing the host can see. With two reminders
  waiting she has to ask which one, and the phone already knows.
- The host cannot look at the phone's screen: ADR 0197 gives it no way to pull anything from the
  phone, so the phone has to say.
- The title shown on the phone can be a third party's text, such as an invite's subject, and
  anything put in her request goes to the model.
- The tools for the work exist. `list_reminders`, `set_reminder` and `cancel_reminder` move a
  reminder, and `update-calendar-event` moves an event through the usual confirmation card.
- A turn's opening row is per turn and per device. Her state block for the turn is built from it,
  and with `session.replay_sent` on (off as shipped) the block is replayed with the turn in the
  history.

## Decision

Let a paired phone send, with the words of a turn, the item it has open; keep it on the turn's
opening row and tell the model about it in one line, as a label, with no new tool and no new
permission.

Its limits:

- **Only a paired device may send it.** The local key sending it is refused (400); the Mac has no
  phone screen to speak for.
- **It is checked twice.** At the door (422 over HTTP, `bad_say` on the socket) and again when the
  line is written. A malformed item refuses the whole turn and is never repaired: a chip on the
  phone that says one thing while she acts on another is worse than an error the phone can show.
- **An id the host gave the phone is kept whole or refused.** It is never cut, because a cut id
  names a different item.
- **The line is the title's only way in.** One line, the title quoted, quotes inside it turned
  into single quotes, at most 80 characters, and it says the title is a label shown on the phone,
  not a request to her. The line also says that "it" or "this", when his words name nothing else,
  mean the item.
- **The item grants nothing.** A reminder is moved by setting the new one first and cancelling the
  old id after, so a failure between the two leaves the old one waiting. An event is changed by its
  id through the confirmation card after she has read it. Entries of the day line are facts.
- **Each turn carries its own item.** The phone sends the item on every turn while its chip shows,
  so "再推一小时" right after still knows which item is meant. Where `session.replay_sent` is on,
  the earlier turn's line also stays in the history as it was sent.

## Alternatives rejected

- **A `snooze` or `move` tool.** It would need a branch per kind (reminder, Outlook event, task)
  for what `set_reminder`, `cancel_reminder` and the calendar tool already do, and a second
  confirmation path for events. The live run with a reminder, `about` and "把它推到明天" leaving
  exactly one reminder due tomorrow at the same clock refutes this if it passes.
- **Put the item in the words, as a prefix the phone adds to `text`.** It would be stored as his
  words in memory.db, replayed as his words, and shown in the conversation window; the title would
  then be indistinguishable from what he said.
- **Drop a malformed item and run the turn.** The phone's chip stays up and she asks "which one?"
  or, worse, answers about the wrong item with no sign to the phone that anything was lost.
- **Truncate a long id.** A Graph id differs from another only in its tail; a cut id can match a
  different event.

## Consequences

- The title's words reach the model and so leave the Mac (`docs/spec.html#egress`). A title written
  like an instruction is not detected; the label wording and the confirmation card are what stand
  between it and an action.
- With `session.replay_sent` off, a turn sent after the chip is cleared does not know the earlier
  item; the history holds his words and her answers, not the state block. A chip left up on the
  wrong item makes "it" mean that item. Clearing the chip is the phone's job.
- Each opening row carries up to about 500 bytes more (a 256-character id and a 200-character
  title), and the replayed state block carries the line of every turn that had one.
- An item is only as good as the id the phone was given: an event id from before an edit is read
  with the calendar tool by id before it is changed, and the answer may be that it is gone.
