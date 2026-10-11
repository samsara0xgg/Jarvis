# ADR 0228 — The phone shows what she is doing from the brain's own step labels, over the socket it types on

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- The owner approved a phone design on 2026-10-11: a typed or spoken message gets a "typing" line
  at once; the first tool she uses turns it into what she is really doing, with elapsed seconds
  (a transit lookup to a place, a web search for a phrase); her first words replace it; under the
  answer a small line summarizes the steps and opens to list them.
- Every surface must show the same words, so the brain writes them from a fixed table, never the
  model (ADR 0008 D6, ADR 0115).
- What exists:
  - ADR 0115 folds the action rows into one line for the Mac, "Working on it..." for most tools,
    and drops the tool's name, arguments and times. Its lines are sentences, not labels.
  - `/phone/ws` (ADR 0209, 0215) streams the answer rows of the turns a device opened, and
    `say` with `spoken: false` already means typed words answered in rows only.
  - The phone sends typed messages with `POST /inherent/attachments` and `POST /inherent/submit`
    and polls `GET /inherent/conversation` every 500 ms. It opens the socket for voice only.
  - A tool call is `action.proposed` (name, arguments), `action.running`, then a terminal row
    that carries only the `action_id`. Her answer's memory record has the id of its
    `surface.response_emitted` row.
- Arguments can be private: a file path, a mail, a reminder's words, where he is. Only a few say
  what she is doing: a search query, a transit destination, a calendar range.

## Decision

Send the phone a `step` frame for each tool call of its own turns, voice or typed, and give her
answer's conversation row the steps read back from the log; let the phone's typed messages, files
included, use the socket. All three share one table and one fold over the action rows.

- A step has a fixed label per kind of work in the language table (`step.<kind>`), a past-tense
  phrase for the summary line, and for web search, page fetch, transit and calendar reads a detail
  from the arguments, cut to 24 characters. No other tool contributes a detail. An unknown tool
  keeps ADR 0115's generic line; plumbing and card tools (`tool_search`, `get_current_time`,
  `ask_user`, `close_question`, `withdraw_card`) are no step.
- A step starts at `action.running` and ends at its terminal row (`failed` for a failed, timed-out
  or cancelled action or a result with an error), or at the turn's end.
- `step` is a frame of its own, not a `row`: a row is a log event sent as it is, and an action row
  holds the arguments; a step is a projection that changes state (running, then done) under one
  number and holds only the label.
- The steps travel in log order with the rows, read by a second cursor beside the rows' one.
- `say` with `spoken: false` may name stored files. The host checks them with the store that
  `/inherent/submit` uses and writes the turn that route would, then acknowledges it as it does
  every typed say.
- The conversation read adds `steps`, `steps_ms` and `steps_summary` to her answers whose turn ran
  a tool, folded from the log on each read.

## Alternatives rejected

- **Extend ADR 0115's `tool` op and its single line.** It keeps one latest line, forgets finished
  tools and has no argument or time, so the summary and the elapsed seconds cannot come from it;
  a second tracker would have the Mac and the phone disagree.
- **Forward the action rows as `row` frames and let the phone build the labels.** The rows hold
  raw arguments (a file path, a mail body) that would leave the brain, and each surface would word
  tools differently; the brain's language setting would no longer decide the words.
- **Write steps into the conversation records when the turn ends.** A tool the brain restarted in
  the middle of would leave no record, and the table could never be corrected for old turns; the
  log already holds every row and a page of at most 500 rows reads in one indexed query set.
- **Let the model name each step.** One more call per step on the wait it describes (ADR 0008 D6).
- **Keep typed messages on HTTP and poll for steps.** A poll at 500 ms shows a four-second step
  at best late, and cannot carry a running to done change without a diff the host would also keep.

## Consequences

- The phone's rows and steps cost two small queries per 50 ms per connection instead of one.
- A new tool shows as the generic line until it gets a kind in the table; a new kind needs both
  wordings in `lang.py`. The summary phrase's tense and joining words are the table's, not
  grammar, so a language added later writes its own.
- A tool already running when the socket opens shows no step, as with ADR 0115's line.
- A step that is still running when its turn ends is closed `failed`; the log has no better word
  for a tool whose end row was lost.
- The summary is in the language set when the page is read, not when the turn ran.
