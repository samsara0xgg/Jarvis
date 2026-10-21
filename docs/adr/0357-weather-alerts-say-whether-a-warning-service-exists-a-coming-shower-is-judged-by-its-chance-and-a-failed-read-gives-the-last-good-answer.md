# ADR 0357 — Weather alerts say whether a warning service exists, a coming shower is judged by its chance, and a failed read gives the last good answer

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0285, `alerts: []` meaning no warnings wherever the place's country is known, alerts keeping no text, and a WeatherKit failure leaving the weather empty with no fallback

## Context

- `alerts` is `[]` wherever the request carried a country. WeatherKit runs warning services in some
  countries only, and the code never asks which, so `[]` reads as "no warnings" where nobody issues
  any. An alert keeps its headline, severity, area, source and times, but not its text.
- `next_hour` counts a minute as rain when its intensity is above zero, whatever its chance.
  WeatherKit's own summary of the hour, period by period with a chance, only names the kind of
  rain. She once said rain would start at 9:52 while Apple Weather showed none.
- A failed WeatherKit read is an error that sends her to `web_search` (ADR 0285: no fallback). A web
  search finds pages, not a forecast: on 2026-10-16 it found Toronto's current conditions and no
  forecast for the next day (ADR 0282).
- Answers are held 5 minutes, so the home's weather line and a turn's call share one request
  (ADR 0285). Nothing is kept past that.
- WeatherKit answers which data sets it offers at a point, and gives each alert's full text by its
  id.
- On 2026-10-21 Allen approved the plan for this tool (「就按你说的去吧」): first find out whether
  the place has a warning service, keep the warning's text, judge rain by its chance, and on a
  failure give the last good answer with its time.

## Decision

Say whether the place has a warning service, judge a coming shower by its chance, and answer a
failure with the last good answer and its age.

- **A service or none.** Before an answer says there are no alerts, WeatherKit is asked which data
  sets it offers at the place, held for a day per place. Where alerts are not offered, `alerts` says
  that no official warning service covers the place.
- **The warning's words.** Each alert carries its own text, and when it starts and ends.
- **A shower by its chance.** `starts_at` and `stops_at` follow WeatherKit's summary of the next
  hour. Without a summary, a minute counts as rain only when its chance is at least half and its
  intensity above zero.
- **The last good answer.** When WeatherKit fails, the answer for the same place and days read in the
  last 6 hours is returned, with the time it was read and why a new one could not be. With none, the
  error says the weather is unavailable and does not send her to a web search.

## Alternatives rejected

- **Keep `[]` and add a note that some countries have no service.** She would have to know which
  countries; one field that says so for this place is the fact itself.
- **A second weather source on failure.** Allen chose one source (ADR 0285, 「不用整两个吧」). A held
  answer from the same source, with its time, keeps one source.
- **Count a minute as rain at any chance above zero.** That is the 9:52 shower that never came.

## Consequences

- An answer for a place not asked about that day makes one more small request, and each alert in
  force one more for its text.
- A held answer can be six hours old. Its time is in its first line, and whether she says it is hers.
