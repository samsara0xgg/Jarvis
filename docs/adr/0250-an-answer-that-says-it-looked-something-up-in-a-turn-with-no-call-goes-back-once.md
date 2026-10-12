# ADR 0250 — An answer that says it looked something up, in a turn with no call, goes back once

**Status:** Accepted
**Date:** 2026-10-12
**Supersedes:** the clause of ADR 0240 that lists which answers it sends back (a claim that something was saved, set, sent, shown or cancelled, and an announced lookup). Everything else in it stands.

## Context

- ADR 0240 sends back an answer that says something was done with no write behind it, and one that
  only announces a lookup ("I'll check"). An answer that reports a lookup in the past tense passes.
- Replay exam, 2026-10-12, Haiku, turns with no tool call: 「这周……我查了你的默认日历，目前没有安排」
  and "Your calendar is clear tomorrow". The calendar was in no block of the state, so both were
  made up, and each was spoken as fact.
- The prompt already says to call a tool for his own data and never to reply that the model will
  check. The model kept the second half and dropped the first by saying it had checked.
- The state block holds some of his data, so an answer from it needs no call: "Nothing new: no mail,
  reminders or commits", the pending reminders, the bus ride, what he is doing now (ADR 0249).
  Those answers must not be sent back.
- A spoken answer is heard as it streams (ADR 0008): what was said stays said, as in ADR 0240.

## Decision

Send a turn-ending answer back once, under ADR 0240's rules and note, when its spoken part says it
looked something up and the turn made no call at all: a lookup verb done (「查了」, 「查过」,
"I checked", "I looked it up"), a look at one of his own stores (「看了一下你的日历」, "I looked
through your inbox"), "according to your calendar", or his calendar said to be clear or to hold
something. A question, a denial and a sentence that cites the program's state are not claims.

## Alternatives rejected

- **Read what his mail, reminders and notes are said to hold, as the calendar's contents are.**
  The state block carries exactly those, so "no new mail" or "a reminder at 9" is a correct answer
  with no call; the table of answers keeps such sentences as rows that must pass. Only the act of
  looking, and the calendar's contents (no block holds them yet), are read.
- **Send back any answer with no call that names a store of his.** Plain talk names them all day
  ("你的日历有点乱", "your calendar app is open"); the table has rows of that kind, none of which a
  name-only rule lets through.
- **Leave it to the prompt.** The prompt already carries both halves of the rule, and the exam's
  answers came with it.
- **Judge whether the answer is true.** Nothing in the loop knows what his calendar holds; the one
  thing it can see is that no call was made.

## Consequences

- The phrase list stays closed, as in ADR 0240: a lookup claimed in other words passes.
- An answer that reads the calendar line of ADR 0249 back as "your calendar shows ..." is sent back
  once and the model calls the calendar tool; saying "you have the dentist at 3:30" does not.
- A claim of a lookup made in an earlier turn is sent back once too, and the note lets the model say
  it was done earlier. The turn then costs one more request.
- A turn with any call, even a refused one, is never sent back for this case.
