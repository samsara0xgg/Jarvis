# ADR 9901 — The writer reads the raw screen text when it needs it, and gets no work-state summaries

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** none

## Context

- The doing-lane writer (ADR 0302) was given, among its clues, "screen" items: the work state's
  English summaries of what a cheap model saw on his screen. In the 10-19 replay it read 放映厅
  as watching a film, gave a kinship term wrong, and folded WeChat into the stretches around it.
  Each error came from what it was given: a summary had already turned his own words into a
  guess in another language, and nothing showed the screen itself.
- TimeSink keeps the text of every front window, one capture row per distinct content, with the
  span it was taken in (ADR 0294 ships them to the brain). A day is far too much text to put in
  every brief.
- His rule (2026-10-21): fix a wrong judgment by fixing the material, never with a rule or an
  example in the prompt. The prompt says the task and the format.

## Decision

Drop the work state's summaries from the writer's clues. Give the writer a tool, `read_screen`,
that answers with the screen text TimeSink read between two of the brief's times: of one key
(NEW KEYS or VARYING KEYS, by its label) or of every window.

- **Lossless.** The text is given whole, window (app and title) by window, each line with the
  moment it was first seen. Only a line identical to one already given for the same window in
  the same read is left out; nothing that differs by a character, and nothing across windows.
- **Paged.** A long read comes in pages of at most 12,000 characters, cut at line ends; each page
  after the first reopens its window. A call names the page it wants.
- **Bounded.** At most 8 rounds of reads per answer, and at most 120,000 characters of screen
  text over them; a read past the characters is told they are spent, and past the rounds the
  writer is told to answer and given no tool. A bad time, key or page, or a store that cannot be read, is answered as an error
  and the writer goes on.
- **Cached.** The day's brief is the same prefix on every round, so the API's prompt cache bills
  it once at full price per answer.

## Alternatives rejected

- **Keep the summaries, better worded** — another model's reading stands between the screen and
  the writer; the 10-19 errors were in that reading.
- **Put the day's screen text in the brief** — tens of thousands of lines a day, mostly unneeded.
- **Merge near-identical lines** — a changed character can be the word that matters.

## Consequences

- An answer costs more when the writer reads: each round re-sends the conversation (mostly cache
  reads) and adds the pages read. 10-19 has 787,159 characters of screen text in 171 windows; the
  character cap bounds an answer's reads to about a sixth of that, about $0.3–0.6 of Opus cache
  writes and reads at most. The replay script prints the rounds and the cost.
- The work state still runs for her other uses; only the writer stops reading it.
