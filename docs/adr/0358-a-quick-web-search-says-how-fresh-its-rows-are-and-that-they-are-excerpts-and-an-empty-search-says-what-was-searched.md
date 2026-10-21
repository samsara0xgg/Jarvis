# ADR 0358 — A quick web search says how fresh its rows are and that they are excerpts, and an empty search says what was searched

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0011, `web_search` returning numbered `title — url — snippet` rows that say nothing of their dates or of what they are

## Context

- A quick search sends the provider the query and a result count, nothing else: no news topic and
  no time range (`_tavily_search_backend`). Its rows carry no date. A 「最新」 or 「今天」 question is
  answered from whatever pages rank first, of any age.
- The rows are the provider's excerpts, up to 1,500 characters each, and no page is opened. The
  description calls them "text rows", and the `tools.web` settings speak of page text. Nothing in
  the result tells her that an answer rests on excerpts.
- 2026-10-16, 「早茶哪家最好，上网多方面比较」: she compared the places from search excerpts alone.
  The deep search came the next day (ADR 0289).
- An empty search returns `{"results": []}`, with nothing on what was searched, and reads as "there
  is none".
- The description sends nothing elsewhere: a place, the weather and a page he names all go to a
  search. `query` is described as "Search query text.", so a deep search gets keywords rather than
  his question and its conditions.
- A quick result can reach 8,192 characters. `docs/spec.html#tools` keeps a result to about 4,000,
  cut by whole rows, and asks for an empty result to say what was searched and what to try.
- Tavily's search takes a topic (general or news) and a time range, and its news results carry a
  published date.
- On 2026-10-21 Allen approved the plan for this tool (「就按你说的去吧」): a date on every row; code
  picks news and the time range from his words; a first line such as 「5 条摘录，没打开网页，最新是
  10-16」; an empty search says what was searched and how to change it.

## Decision

Make a quick search say how fresh its rows are and that they are excerpts, and make an empty search
say what was searched.

- **How fresh.** `web_search` takes `within` (a day, a week, a month or a year) when his words ask
  for something recent, and `news` when he asks for news or events. Code turns them into the
  provider's own time range and topic. Each row carries its published date where the provider
  gives one, and says the date is unknown where it does not.
- **What it rests on.** The result opens with one line written by code: how many rows, that they
  are search excerpts and no page was opened, the newest date among them, and the range asked. An
  example: "5 search excerpts for 'BC news', none opened; newest Oct 16; past day."
- **An empty search.** No rows is said as such: what was searched, with which range, and what to
  try (other words, a wider range, or a deep search).
- **To the standard.** The description says when not to search: a place goes to `map_search`, the
  weather to `weather`, and a page he names to `web_fetch`. `query` takes his question with its
  conditions, in his words. Rows are cut whole to keep the result to about 4,000 characters.

## Alternatives rejected

- **Open the top pages in every quick search.** The provider's whole page starts with navigation:
  against the per-row budget it bought "[Skip to content]" where the excerpt held the answer
  (`_tavily_search_backend`'s note). Opening a page is `web_fetch`'s job and the deep search's.
- **Guess freshness from the query's text with a word list in code.** A list of 最新, 今天 and
  "latest" misses phrasings in two languages and cannot tell 「最新的 Rust 版本」 (a month will do)
  from 「今天的新闻」 (a day). She reads his words into `within` and `news`; code sets the provider's
  filter from them.
- **Leave the dates out and let her infer them from the excerpts.** Excerpts rarely carry their
  date, and an undated row about a price reads as today's.

## Consequences

- Rows from a general search mostly say their date is unknown, which is true.
- A time range can hide an older page that held the answer. The empty result names the range, so a
  wider search can follow.
- Whether she sets `within` and `news` when his words call for them rests on the description, and
  the whole-turn exam watches it.
