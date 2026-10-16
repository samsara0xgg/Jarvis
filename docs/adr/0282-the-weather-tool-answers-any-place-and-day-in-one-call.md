# ADR 0282 — The weather tool answers any place and day in one call

**Status:** Superseded-by-0285
**Date:** 2026-10-16
**Supersedes:** 0188

## Context

- 2026-10-16, live: after "明天天气怎么样" he said "我说的是多伦多的". The `weather` tool took no
  arguments and held only home, today and tomorrow (ADR 0188), so its description sent another
  city to `web_search`. The search found Toronto's current conditions only, and she said she
  could not give tomorrow's forecast. A weather question about any place or day is the common
  case, and it should be one call.
- Open-Meteo is already the weather source and an allowed egress (ADR 0051, ADR 0188). Its
  forecast takes any latitude and longitude and a day range, its geocoding API turns a name in
  any language into one, and its air-quality API answers for the same point.
- `where_am_i` and `bus` already read where he is, from this Mac or from his phone's last report
  (ADR 0194, 0198, 0212); `bus` takes `here` for it.
- ADR 0195 gave a home weather line its own Jev group, which says no wait line, and the
  description it wrote forbids stating one place's weather from another's result.

## Decision

`weather` stays one read-only L1 model tool on Open-Meteo, with optional arguments, and no
arguments is still the home's now, today, tomorrow and next 12 hours.

- `place`: a name in any language, geocoded as given (in the name's own language, then English);
  `here` for where he is now, read as `bus` reads it; `lat,lng` for a point. Omitted is home.
  The result names the resolved place and its time zone; when other places share the chosen
  one's name it lists up to two, with coordinates that `place` accepts. An unknown place is an
  error.
- `start_date` and `end_date`: any day or range from 7 days back to 16 days ahead; outside it is
  an error that sends the model to `web_search`.
- `hourly` gives every hour of the days asked (at most 3 days); `air_quality` adds US AQI, PM2.5
  and PM10.
- Its description says it covers every place and date in range, that the model passes the place
  he names, again when he corrects it, and that `web_search` is for dates beyond the range and
  for alerts and news.
- Each read that fails (the place lookup, the forecast, the air quality) is an error that names
  it. Timeouts stay at the home view's 4 s.

## Alternatives rejected

- **Leave other places and later days to `web_search`.** The 2026-10-16 turn: the search answered
  with current conditions only, and no forecast of tomorrow, in 1 of 1 live tries.
- **A separate tool for other places.** Two tools split by "is it home" put the home/away
  decision on the model for every weather question; one tool with an optional `place` leaves
  it with the words he said.
- **Judge the date range in the place's own zone.** The zone is not known before the first
  request for home, `here` and a point, and a pre-request check needs no extra call; it is
  judged on the UTC date with a day of grace, and Open-Meteo refuses what is still out.

## Consequences

- A named city is still grouped under `web_search` by Jev (ADR 0195), so a wait line ("looking
  it up") can play before `weather` answers for it; widening the `home_weather` group is a Jev
  change that needs its own eval.
- Open-Meteo now receives the name he said, or his coordinates for `here`, not only the home's.
- One call can make up to three sequential requests, each with a 4 s timeout.
