# ADR 0355 — The weather says what fell earlier today, what is left of it and tonight, and a plan reads an outing's own hours and place

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0285, the answer keeping the old fields, so that `today` is the whole day's daily row with no label and a request with no days gives the next 12 hours

## Context

- `weather` with no days answers `today` and `tomorrow` from WeatherKit's daily rows. A daily row
  covers midnight to midnight: one chance, one amount and one condition for the whole day, and
  nothing in the answer says so. The hourly rows start at the current hour and stop 12 hours later
  (`report` in `jarvis/runtime/weatherkit.py`).
- What that did:
  - 2026-10-17 morning, 「外面下雨吗，能早点出门吗」: she said there would be drizzle today and to
    take an umbrella. The drizzle had fallen before dawn, and every hour from then on was 0%. Any
    overnight shower does it again.
  - The description says the call with no days answers "tonight" and must not be repeated. Asked
    at 09:34, its 12 hours end at 21:00, so a morning 「今晚冷吗」 gets the dawn low or tomorrow's.
- A day row carries its date and no weekday. `docs/spec.html#tools` asks for local times with the
  weekday and today or tomorrow, and for a first line she can say as it stands.
- The plan's opening (ADR 0288) and its `outing_soon` notice (ADR 0291) read the weather through
  `Home.day_weather`: the whole day's row at home. An outing at 15:00 across town is told the
  morning's drizzle at home.
- WeatherKit takes the first hour of its hourly rows as a request parameter, and the same request
  serves the home's weather line (ADR 0285). Its daily rows split a day at 07:00 and 19:00 into a
  daytime and an overnight part.
- On 2026-10-21 Allen approved the plan for this tool (「就按你说的去吧」): code works out from the
  hours what has already fallen, the rest of today and tonight; the whole day is labelled as the
  whole day; the call with no days covers tonight; plans and outing notices read the outing's hours
  at its place.

## Decision

Answer today in parts that code adds up from the hours, and give a plan the weather of each
outing's own hours and place.

- **Today in parts.** For today the answer reads the hours from local midnight and gives, all
  computed in code:
  - `earlier_today`: what fell since midnight, how much, and until when;
  - `rest_of_today`: from now to midnight, the highest chance, the amount, and from when to when
    rain is likely;
  - `tonight`: 19:00 to 07:00, Apple's own split of a day, with the low and its hour and the chance
    and amount of rain.
  The daily figures stay, named as the whole day's.
- **Labels.** Every day row carries its weekday, and today, tomorrow or yesterday where it is one
  of those.
- **Through tonight.** A request with no days covers now, today in parts, tonight and tomorrow. Its
  hourly rows run to the end of tonight within the result's size limit.
- **One line first.** The answer opens with one line that code writes from those figures, such as
  "Victoria: 9° and cloudy now; about 1 mm fell before 7:00; no more rain today; tonight's low 6° at
  6:00, dry."
- **An outing's own weather.** The plan's opening and each `outing_soon` notice read, for each
  outing, the hours from its leave time to its end, at its place:
  - the place's position when it is known (a kept place, or the end of its leg);
  - else home, and the facts say it is home's.
  The draft and the opening also get the day in parts at home.

## Alternatives rejected

- **Keep the whole-day row and label it as the whole day.** A label does not say the drizzle is
  over. On 2026-10-17 the 0% hours sat next to the daily row, and she still answered from the row.
- **Make the default hours 24.** A 24-hour answer still says nothing about what already fell, and
  it runs past the result's size limit: a three-day hourly answer is 10,500 characters by the audit
  of 2026-10-20, against about 4,000 in `docs/spec.html#tools`.
- **Let the model add up the hours.** It had the hours on 2026-10-17 and did not; adding up is
  code's job by `docs/spec.html#tools`.

## Consequences

- An answer for today asks for up to 24 more hourly rows in the same request.
- The parts are computed, not quoted from Apple: a wrong rule in the sums is wrong in every answer.
- Tonight starts at 19:00. A question about 17:00 is answered from the hours.
- An outing whose place has no known position gets home's weather, labelled as home's.
