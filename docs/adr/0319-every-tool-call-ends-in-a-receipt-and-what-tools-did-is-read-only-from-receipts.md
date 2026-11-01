# ADR 0319 — Every tool call ends in a receipt, and what tools did is read only from receipts

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** 0267. Also: In part, ADR 0044: what this Decision changes there (named by the 10-25 audit). Everything else in it stands.
**Area:** tools-receipts-cards

## Context

- Real use from 2026-10-12 to 10-20: 20 of 43 tool asks went wrong, and 14 of those 20 were facts a
  tool returned wrong or short (ADR 0317). Allen's ask of 2026-10-20 is that every tool's result be
  trustworthy enough that he asks her instead of opening an app. On 2026-10-21 he approved the plan
  this foundation belongs to (「就按你说的去吧」).
- A call counts as a success when it raised nothing (`TurnCall(..., ok=error is None)` in
  `jarvis/decision`). A tool that stops to ask returns a normal result: `bus(get_off=…)` with two
  matching stops says "Ask him which" (`bus_tool.py` :478, :489), counts as done, and lets
  「下车提醒设好了」 pass the guard.
- Read or write is fixed per tool (`Tool.read_only`), but `schedule` and `bus` read or write
  depending on their arguments. A read of either counts as a write: it waits out the hold before
  dispatch and backs every claim for the rest of the turn (`turn_guards.py:215`). `shared/steps.py`
  already tells a calendar edit from a read and a ride from a transit look by the call's arguments.
- An error reaches Anthropic as an ordinary `tool_result` with no `is_error` (`llm_anthropic.py`,
  `to_messages`), and its code is whatever the tool chose: `invalid_argument` at 43 sites,
  `invalid_arguments` at 50, `bad_arguments` at one. The spec already names four kinds of failure.
- A result over its cap (8192 characters unless the tool says otherwise; the spec says about 4000) is
  fitted by keeping the head and tail of its longest strings (`tools.py`, `_fit_result`). The rows
  in the middle of a list vanish, leaving only `truncated: true`, so "nothing at 2 pm" can be false.
- Across turns the history carries `[You did: worked on reminders]` (ADR 0267): kinds of steps, not
  which object or what was set. On 2026-10-19 19:25 an add said it rings 60 minutes before
  (`f8adc1`). A later read that showed no rings made her retract the true claim. Reads now show rings
  (main 95478378), but the history line still could not have told her.
- A turn running beside another sees only the other's words, "Still being answered in another turn".
  It does not see what that turn has already done. On 2026-10-20 15:39, 「谢谢谢谢」 got
  「那五个提醒还在处理」.
- Every call already ends in one `action.result_observed` event, written by the dispatcher. It holds
  the serialized result, up to its cap, and an error code.

## Decision

Make every tool call end in a receipt: the tool's own account of what the call did, kept in the
call's `action.result_observed` event. Take three things from receipts and nothing else: whether a
call worked, what earlier and concurrent turns did, and what a claim may rest on (ADR 0320).

Its limits:

- **What a receipt says.**
  - Whether the call only read or changed something.
  - For a change: the object it touched, with the short id she sees, and the undo id where there is
    one.
  - One line she can say.
  - One status:
    - done;
    - pending: accepted, but not yet confirmed, such as a command sent to a device that has not
      answered;
    - needs a choice: the tool stopped to ask between candidates;
    - failed, with one of the spec's four kinds.
  - Only done counts as success.
- **Read or write per call.** A tool declares, from a call's arguments, whether that call only reads.
  The hold before dispatch, the receipt and the checks all use that. A tool that declares nothing
  keeps its fixed flag.
- **Who writes it.**
  - A built-in tool writes its own receipt.
  - A tool that writes none gets one derived by the dispatcher, as every plugin tool does. A call is
    a read when the tool is marked read-only. Otherwise it is done on an answer that is not an
    error, and failed on an error. A derived receipt has no undo id.
  - A tool rebuilt under ADR 0317 writes its own, and its tool-layer items assert it.
- **A read says what it covered.** The sources it read and the window or filter it used. Rows it does
  not show are counted, with how to get them. A list is cut at whole items, never by keeping the
  head and tail of its text.
- **A failure is an error to the model.** A failed receipt reaches the model marked as an error
  (`is_error` on Anthropic's `tool_result`).
- **One projection.** Receipts fold from the log into a projection by turn and by object. It is
  rebuilt from events like every projection (I3), and both she and the checks read it.
- **History.** After an answer whose turn has receipts, the program adds a line of them: each
  receipt's line, id and status.
  - It keeps ADR 0267's rules: English, read at render time, fixed once the turn ends, not her
    words, and cut from her reply like `<react>` when she copies it.
  - A read shows what it covered, not what it found.
- **A turn in flight** sees the receipts the other turn has written so far, under the line that
  names that turn's question.

## Alternatives rejected

- **Keep success as "raised nothing" and make each soft failure an error.** A stop between two
  candidates is not a failure: the ride stays valid and the next step is to ask him. Calling it an
  error makes the four kinds lie, and a read would still count as a write.
- **Derive every receipt in the dispatcher from the result.** 49 tools return 49 shapes. Only the
  tool knows whether its write took: "sent to the Mac" and "shown on the Mac" look the same to the
  dispatcher.
- **Show receipts in the state block instead of after each answer.** The block is rebuilt every turn.
  A receipt would change place as new ones arrived, older ones would need a cutoff, and the receipt
  would no longer sit next to the answer it backs. The history line keeps the prefix cache
  byte-stable, which is ADR 0267's reason for the place.
- **Put whole results back into the history (ADR 0027).** Results are the large and private part. A
  receipt is one line.
- **Raise the cap instead of cutting at whole items.** 8192 is already twice the spec's figure, and
  the next longer list loses its middle rows silently under any cap.

## Consequences

- Every built-in tool's return changes. Until a tool writes its own receipt, it gets the derived
  one, and a soft failure inside it still reads as done.
- A plugin write is done on its server's word. A server that answers OK without acting still passes.
- A turn that writes five reminders carries five receipt lines in the history.
- Pending is a new outcome. She has to say it in words ("sent, not confirmed yet"), and the phone's
  step line (ADR 0231) needs a pending phrase in the language table.
