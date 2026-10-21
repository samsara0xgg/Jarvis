# ADR 0343 — A bus answer gives the bus he can run for, the first he can catch and the one after, each on its day

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- In his use the bus he needed was cut, or was listed and not said:
  - 2026-10-14 09:29 the 09:31 he was walking to was missing. ADR 0265's look-back brings such a
    bus back only when he is 2 to 5 minutes short of a normal walk. Even then the summary skips
    it: it starts from the first option with more than -2 spare minutes ("option 2: leave 9:51").
  - In a replay of another answer, one route's bus at 09:36 stayed, while another route's at 09:53
    and the first route's next bus at 10:02 were dropped. Any option arriving more than 15 minutes
    after the earliest is cut as far-fetched. The next bus of a route that runs every 20 minutes always arrives about 20
    minutes later.
- Times are kept as HH:MM and put back on today's date wherever they are used. At 23:50 a bus at
  00:10 is placed 23 h 40 min in the past, its spare is about -1420 minutes, and the answer is "No
  bus route found" (exam item `bus-after-midnight`).
- When Here cannot confirm where he is, the result carries a warning field (ADR 0322). The summary
  leaves it out, so times can be given from an old start with no caveat.
- Google returns a few alternatives around the time asked. For a route that runs every 30 minutes,
  the bus after the first catchable one is often not among them. The timetable store (ADR 0342)
  has every trip.
- The spec asks a result to give times in local time with the weekday and today or tomorrow, and
  the model to compute no dates (`#tools`). The `day=YYYY-MM-DD` argument makes it compute one.
- A trip Google finds only on foot becomes `not_found`, and the walking and driving times already
  fetched for it are lost.

## Decision

Lead every bus answer for leaving now with what he needs to decide. That is the bus he can still
run for, the first bus he can catch at a normal walk, and the one after it, each with its full date
and time.

- **The three.** In departure order:
  - the latest bus he would miss at a normal walk by no more than five minutes (ADR 0265's run-for
    bus);
  - the first catchable bus;
  - the next bus after that one, from any route Google returned whose ride is no longer than 1.5
    times the shortest.

  The cut by arrival time goes.
- **The next bus from the timetable.** When Google's answer has no bus after the first catchable
  one, the timetable gives the next trip of the same route, from the same stop, in the same
  direction, with its arrival at the same alight stop. When the first catchable bus is on the next
  service day, the answer also says when the last bus of that route left.
- **Dates.** Every time stays an aware local date and time, from the source's stamp to the
  sentence; nothing puts a date back on a clock time. A time is said with its day whenever it is
  not today. A time he gives for `at` or `arrive_by` is a local date and time, or a clock time
  alone. A clock time alone is today's, or tomorrow's once today's is more than an hour gone. There
  is no day argument.
- **The summary.** It says these in order:
  - that the start is old, when Here could not confirm it;
  - the run-for bus, as one he can only make by running;
  - the first catchable bus, with when to leave;
  - the one after.

  The parameter descriptions carry no rules about the result.
- **Walk only.** A trip Google finds only on foot answers with its walking and driving times
  instead of `not_found`.
- **Other lists.** A named route, `arrive_by` and a later time keep their own lists (ADR 0221,
  0288). They take the dates, the timetable match of ADR 0342 and the order of the summary.

## Alternatives rejected

- **Widen the far-fetched cut from 15 to 30 minutes.** A route every 30 minutes still loses its next
  bus, and a slow detour arriving 25 minutes later still passes. The cut measures arrival when the
  question is departure.
- **Ask Google again from the first catchable bus's departure.** That is one more paid Routes Pro
  request on every answer, and Google may still return a different route. The timetable has the
  same route's next trip at no cost.
- **Keep HH:MM, and move a time that looks past to the next day.** A missed bus also looks past,
  so the run-for bus would become tomorrow's.
- **Let the model put the run-for bus first.** This is today's design: the rule sits in the `to`
  description ("say so, then give the next bus"). The summary still led with option 2, and the
  model speaks the summary.

## Consequences

- An answer can hold a bus he cannot reach at a walk. The card still offers only catchable buses,
  and pinning a missed one is refused (ADR 0265).
- The timetable's next trip can differ from what Google would plan for that time, such as a
  faster route a minute later. It is said as the same route's next bus.
- Every user of the clock times changes with the dates: the card's rows, the pin, and the planner
  behind a day plan's legs (ADR 0288).
- The five-minute run and the 1.5x ride are heuristics, as before.
