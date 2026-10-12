# ADR 0242 — What he and Jarvis said is one read-only tool that searches, summarizes and opens

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** the clause of ADR 0142 that has `recall` return the day summaries to the model. Everything else in ADR 0142 stands.

## Context

- The model had three reads of the conversation store (`memory.db`): `search_records` (words and
  a time window, cursor pages, relevance or newest first), `recall` (a day or range: the day
  summaries, then one line per record, a 200,000-character page budget) and `read_records`
  (original text by id, in chunks, cursor).
- Live data on the Pi brain, 2026-10-11: `search_records` was called 22 times in one turn;
  across `get_briefing`, `recall`, `read_records` and `search_records` there were 27 cursor
  errors. Models invent cursors ("x", "-", "eyJub25lIn0="), retype a real one with a corrupted
  hash, and resend the identical call after "Invalid cursor". A cursor is a string the model
  must copy exactly.
- The same models choose wrongly between six tools for "what did we talk about on Oct 1" and
  "what was the first thing I told you last month"; the second took 22 pages from the newest end
  before `order=oldest` existed (live 2026-10-02).
- Every past day already has one derived summary with a `### Topics` list, written each night
  (ADR 0142); today has none, and a day under 1500 characters is stored word for word.
- The repeat guard (ADR 0239, `turn_guards.repeated_call`) answers a call identical to an earlier
  one with its repeat note; a page number differs on each call.
- A relevance ranking was added to `search_records` because a date digit shared by hundreds of
  lines buried the line holding the rare words (live 2026-10-03).

## Decision

The model's conversation history is one read-only built-in tool, `conversation`, on the menu from
the first request: `query` returns up to 8 lines holding any of its words (best match first,
newest or, with `order=oldest`, oldest among equals) as id, local time, side and a 200-character
excerpt, `page=2` the next 8, recomputed from the store on each call; `date` (with `to=`, at most
14 days) returns each day's topics from its saved summary, and a day without one its own lines;
`date` or `query` with `order` or `speaker` lists the window's lines themselves, so "the first
thing I said last month" is one call; `open` returns the line and two either side, at most 4000
characters. `search_records`, `recall` and `read_records` leave the menu and stay registered,
deferred.

## Alternatives rejected

- **Keep the three tools and rewrite their descriptions.** All 27 cursor errors and the 22-call
  turn happened with the cursor rules already in the descriptions; a cursor the model copies is
  the failure, whatever the text says.
- **Hold the cursor on the server and let the model send `more: true`.** Every page call then has
  the same arguments, and the repeat guard (ADR 0239) answers the third with its repeat note
  instead of running it; a page number differs on each call.
- **Separate `conversation_search`, `conversation_day` and `conversation_open`.** Three entries
  and three schemas on every request, and the model chooses among them before it has seen any
  result, which is where "which tool for last month" went wrong; one tool moves that choice into
  an argument.
- **Newest first without a ranking.** The ranking exists because a date digit shared by hundreds
  of lines buried the line holding the rare words (live 2026-10-03); dropping it brings that
  back, and keeping it costs one pass over the matching lines.
- **A model call that summarizes a window on demand.** It adds seconds and a paid call to a read
  that the nightly summaries (ADR 0142) already answer for every past day.

## Consequences

- `page` rereads the store each call and holds nothing: a line said between two page calls can
  move a line across the page boundary. Ids are the record ids and do not move.
- A day with no written summary (today, or one stored word for word) answers with his own lines,
  marked as such; it is not a summary. `recall`'s day line and highlights (ADR 0235) no longer
  reach the model.
- A line longer than 1800 characters is shown cut, with the count of characters not shown;
  `open` has no way to page through the rest, as `read_records` had.
- Mail records and picture notes are searched with the lines of the conversation and say `mail` or
  `picture note` as the side.
- The three old tools stay registered because tests dispatch them by name, and the history header
  that `render_context` writes still names them; it is changed with the next prompt revision.
  A model that sends `tool_search` a query naming one of them (they are built-ins, not in its
  index) gets the note of ADR 0241's `Tool.supersedes` pointing at the `conversation` call.
