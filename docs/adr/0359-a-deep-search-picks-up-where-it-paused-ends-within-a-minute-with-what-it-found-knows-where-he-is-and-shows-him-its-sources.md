# ADR 0359 — A deep search picks up where it paused, ends within a minute with what it found, knows where he is, and shows him its sources

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- A deep search is one nested request to Anthropic's own web search (ADR 0289). When Anthropic's
  server loop pauses a long turn (`pause_turn`), the request has to be sent again with its own
  transcript to go on; the code never does. A paused transcript that ends on a search, not on the
  brief, fails after the whole wait and cost (`findings_from` in `jarvis/runtime/deep_search.py`).
- The nested request has a 120 s timeout and one retry. The worst case is about four minutes after
  she said 「我认真查一下，大概半分钟」 (ADR 0289), and it ends with nothing. The live check of
  2026-10-17 took 27 to 39 s per search.
- The nested model is told today's date without its weekday, and nothing of where he is. 「附近」 and
  "Richmond" are guesses. Anthropic's web search takes an approximate location (city, region,
  country, time zone), and each page it returns carries the page's age.
- The sources come back as "title — url": no site, no date, and nothing of them reaches his screen.
- Her answer has a spoken part and a written part (ADR 0040). The Mac and the phone draw the
  written part with working links; only the spoken part is said aloud.
- On 2026-10-21 Allen approved the plan for this tool (「就按你说的去吧」): go on after a pause; wrap
  up within 60 s and hand over the pages found even when the brief is unfinished; take his location
  and the weekday; date the sources, and send them to his device.

## Decision

Carry a deep search through its pauses inside one minute, give it his place and the weekday, and
put its dated sources under her answer.

- **Picks up.** A paused nested request is sent again with its own transcript until the brief is
  written or the minute is up.
- **One minute.** The whole deep search has one budget of 60 s, with no retry after it. It streams.
  When the budget ends before the brief, the result is the pages its searches found so far, each
  with title, site and date, marked unfinished, not an error.
- **Where and when.** The nested request carries his approximate location (the city, country and
  zone of where he is by Here, else home's) and today's date with its weekday.
- **Dated sources.** Each source carries its site and the page's date when the search gave one.
- **On his screen.** Code writes the sources under her answer, in its written part and never in its
  spoken part: site, date and link for each, on the device where he asked.

## Alternatives rejected

- **Keep the 120 s timeout and the retry.** The live check took 27 to 39 s. Past a minute he has
  waited twice what she promised, and the retry can double that to end in the same error, while
  the pages already found are thrown away.
- **A sources card in the conversation's card slot (ADR 0205).** A newer card replaces it, so the
  sources would vanish at his next question, and the Mac and the phone would each have to learn a
  new card. Under her answer they stay with the answer they support.
- **Put his location in the question text.** The search provider localizes results by the tool's
  location field; words in the question are only more search terms.

## Consequences

- A deep search that needs more than a minute answers from its pages' titles, not from a brief.
  She can read one of them with `web_fetch`.
- His approximate city goes to Anthropic with each deep search. The egress row changes with the
  code.
- The written part of a deep-search answer grows by a list of sources on every device that shows
  it.
