# ADR 0195 — A home weather line gets its own Jev group, and no wait line

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- After ADR 0188, a home weather question still played the web wait line ("我上网查查")
  before the `weather` tool answered: Jev's `web_search` group named weather, and Jev picked
  it at 0.97-1.0 for every weather line, so the line sounded like a search that never ran.
- Taking "weather" out of the `web_search` text alone did not move Jev: it still picked
  `web_search` at 0.97-0.99 for 今天天气怎么样 / 明天会下雨吗 / 现在外面几度.
- In a replay of the real 2026-10-06 22:49 request, the model sometimes sent 这周末天气怎么样
  to `weather`, which holds only today and tomorrow.

## Decision

- Jev has a `home_weather` group: the weather here, with no city named (now, today, tonight,
  tomorrow, rain, the temperature outside, an umbrella). It is in no `tool_line.groups`, so a
  line Jev puts there says nothing before the tool. `web_search` keeps the weather in a city
  the user names.
- The `weather` tool's description says it holds only today and tomorrow, sends the weekend,
  later days and other cities to `web_search`, and forbids stating another place's weather from
  its result or an earlier answer about home.

## Alternatives rejected

- **A system-prompt rule that live facts come only from a tool result for that exact place.**
  In the same replay it turned 10 of 16 Vancouver lines into a question back ("do you mean
  Vancouver's weather?") instead of a search.

## Consequences

- Jev, 12 lines: every home weather line picks `home_weather` at 0.99-1.0; named cities, news,
  prices and scores stay `web_search` at 0.99-1.0; 这周末 picks `home_weather` at 0.63, under
  any bar. Data in `~/.jarvis/research/weather-tool-2026-10-06/`.
- The model, replayed: weekend 6/6 to `web_search` (4/6 before). A Vancouver line right after
  three answers about home was answered from those answers in 1 of 16 tries (0 of 18 with the
  old description): the description does not remove that, and no prompt line tried did either.
