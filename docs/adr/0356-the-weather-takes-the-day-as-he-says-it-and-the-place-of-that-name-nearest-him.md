# ADR 0356 — The weather takes the day as he says it and the place of that name nearest him

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0285, the model passing `start_date` and `end_date` in the place's own calendar, and a named place taking the geocoder's first match

## Context

- `weather` takes `start_date` and `end_date` as `YYYY-MM-DD`. Its description asks the model to
  work out a weekday or a day of the month from today's date, so 「这周末」 asked on a Tuesday is
  the model's arithmetic. The answer cannot show the slip: it gives the dates it was asked for.
- `docs/spec.html#tools`: times and places are taken as his words and the tool resolves them; the
  model computes no zone. `my_schedule` already takes a day as a word (today, tomorrow, a weekday)
  or a date and resolves it in code (ADR 0248).
- A named place is the first match Open-Meteo's geocoder returns, worldwide. Other places are listed
  only when they have exactly the same name, two at most. Nothing prefers a place near him:
  「Richmond 明天下雨吗」 can be answered for Richmond, Virginia, and 「维多利亚」 can resolve outside
  BC. "Vancouver, BC" goes to the geocoder as one name.
- Each geocoder match carries its coordinates, region and country. Here (ADR 0322) says where he
  is, and the settings hold home.
- On 2026-10-21 Allen approved the plan for this tool (「就按你说的去吧」): take his own words for
  the day and resolve them in code in the place's zone; prefer the place near him; name the region
  and country in the answer.

## Decision

Take the day in his words and resolve it in code, and pick the place of that name nearest him.

- **His words for the day.** `weather` takes `when` in place of `start_date` and `end_date`:
  - today, tonight, tomorrow, the day after, yesterday;
  - a weekday, this week's or next;
  - the weekend, this one or next;
  - a day of the month, a month and day, or a date;
  - two of these for a span;
  in Chinese or English. Code resolves them in the place's own zone and calendar. Words it cannot
  read are an error that lists what it reads.
- **One resolver.** The same code resolves `my_schedule`'s `date` and `to`, which then take the same
  words in his zone.
- **Nearest first.** Among the geocoder's matches for a name, one within 300 km of where he is
  (Here, else home) wins over farther ones. With none that near, the geocoder's first match stands.
  A name followed by a region or a country after a comma is matched on those too.
- **Named in full.** The answer names the place with its region and country, and lists the other
  places of that name that it did not pick, each with what `place` takes to read it.

## Alternatives rejected

- **Keep the dates and give the model today's weekday.** The model still does the arithmetic that
  `docs/spec.html#tools` takes from it, and a wrong date comes back as a correct-looking answer for
  that date.
- **Ask him which one whenever two places share a name.** Names he says are mostly near him; a
  question every time costs a turn in the common case, and the answer already lists the others.
- **Google's geocoder with his position as a bias.** Each request is billed under the monthly cap
  of ADR 0283, while Open-Meteo's matches already carry the coordinates to rank by, at no cost.

## Consequences

- A small place within 300 km is picked over a famous one far away when he meant the famous one.
  The answer names the place and lists the other, so he can correct it in one turn.
- A phrase outside the resolver's words is an error; she can pass a date instead.
- A change to the day words changes both tools at once.
