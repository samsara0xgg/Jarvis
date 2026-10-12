# ADR 0239 — A repeated call is not run again, and an answer no call backs goes back once

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- A replay exam on 2026-10-11 (97 real decision points, three models) caught
  models ending a turn on a claim or a promise with no call: 「记下了」, 「我记住啦」,
  「已记下：10月3日…」, "I've put the findings on screen", 「我查一下10月1日的记录。」,
  "I need to check tomorrow's forecast", with nothing saved, shown or looked up.
  Haiku did it in 16 runs, Luna in 9, Sonnet in 4.
- The same exam caught identical calls repeated inside one turn: `job_ledger` with
  no arguments five times, `create_memo` with one text ten times, `gmail_get`
  re-reading one id, `query_activity` resent unchanged after `result_too_large`,
  `withdraw_card` after "no card is waiting".
- The only guard on repeats (2026-10-06) names five history reads. None of the
  five repeats above is on a tool it names, and one is a write.
- ADR 0019 removed the claim ladder: the pre-emit gate stamps a draft and judges
  no claim, so nothing reads what an answer says against what the turn did.
- A spoken turn speaks each sentence as it streams (ADR 0008), so a first answer
  is heard before any check could run. The request after the tool budget carries
  no tools (ADR 0178), so a note asking for a call cannot be answered there.

## Decision

Refuse a call identical to an earlier one in the same turn (same tool, same
canonical arguments) with a tool-result note instead of running it, unless a
non-read-only call succeeded after that earlier one; and send a turn-ending
answer back once with a note from the program when it says something was saved,
set, sent, shown or cancelled and no non-read-only call succeeded this turn, or
when it only announces a lookup and the turn made no call.

An earlier call that failed is refused too, with a note that names the fix. The
answer goes back once per turn, never on the budget's wrap-up, on the last
request the budget allows, or on a correction run; the model's answer and the
note join the request and the loop runs one more time, on the spoken route as
on the batch route (the first answer stays spoken, the next is spoken after it).
Claims and announcements are read from a short list of Chinese and English
phrases; a sentence that asks is not a claim. A guard that acts is written as a
`turn.guard_fired` event.

## Alternatives rejected

- **Add the five exam tools to the history-read list.** The list is by tool: a
  repeat of any tool it does not name, such as the next one a tool search loads,
  runs again. The exam's five repeats were on five different tools.
- **Refuse every identical repeat, with no exception for a write.**
  `list_reminders` after `cancel_reminder` has the same arguments and a different
  result; refusing it hands the model the list from before the cancel.
- **Judge the claim in the pre-emit gate.** The gate sees text after it is
  written, and on the spoken route after its first sentences are heard; it can
  only reject or rewrite words. The 29 runs lacked a call, which only another
  request can supply.
- **Send the answer back every time it claims.** The note allows "already done
  earlier or cannot be done", so a second claim after it is a considered answer;
  each further bounce is a paid request and, spoken, another answer after the
  last.

## Consequences

- A turn can cost one more request, and on the spoken route Allen hears a second
  answer after the first; a claim already said aloud stays said.
- The phrase list is closed: a claim worded another way passes, and a report of
  state that already exists (「你已设了三个闹钟」 after a read) is sent back once
  and the model has to say so.
- A read of changing state repeated unchanged in a turn (a poll with the same
  arguments) is refused until a write intervenes; the model has to change an
  argument or use the result it has.
- `ToolDefinitionLike` carries `read_only`, which the decision layer now reads.
