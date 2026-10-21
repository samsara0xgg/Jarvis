# ADR 0344 — Bus finds stops by one finder, shows a stop's next buses, and checks a ride's bus against his stop

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0224, matching a said ride to the bus nearest his last fix, and taking a place's stop as the trip's stop nearest it

## Context

- 2026-10-17, on his bus home, he said he would get off at home. The ride had matched the bus of
  his route nearest his last fix, which was a trip that does not pass his stop. His stop's name
  came back `no_such_stop`, because that stop was not on the trip followed. "Home" then gave the
  followed trip's stop nearest his home. No answer named the bus's headsign, which would have shown
  the mistake.
- A said ride takes the route's bus nearest his newest fix, within 300 m. The bus's position is
  read at the fix's moment only when its history already reaches back that far, which needs a
  boarding watch to have been polling. Otherwise the bus's latest position is set against a fix
  that can be a minute older, and at bus speed they can be hundreds of metres apart. His
  destination plays no part in the choice.
- A `get_off` name must appear inside the stop's name as written ("Fort & Cook" does not find
  "Fort St at Cook St"), and stops the bus has already passed are accepted. Home and school are
  resolved, but no other kept place and no address.
- His stop for a place is any stop within 800 m of it, and the distance is never said.
- Asked at home for his route's next bus, `bus(route=…)` plans a trip to home and fails. Nothing
  answers "when does the next one leave my stop". "The nearest bus stop" goes to `map_search`,
  which knows no stop ids and no routes.
- While he rides, a question with a destination plans a new trip instead of reading the ride.
- The timetable store (ADR 0342) holds every stop, the routes and headsigns at each, and every
  trip's stops in order. Places he names are found by one reader (ADR 0346).

## Decision

Give the bus tool one stop finder over the timetable, a departure board for a stop, and a ride
whose bus is checked against where he is, at the same moment, and against where he gets off.

- **One stop finder.** It takes a stop's name or code, a place, an address, or here. It returns
  stops with their distance from that point and the routes and headsigns each serves. A name
  matches by its words, in any order, with "at", "&" and "and" alike. A place's stop is within
  300 m of it, nearest first, with the distance said; when none is, the answer says so and names
  the nearest stop with its distance. The bus tool, the ride and the board all use it, and
  `map_search` sends stop questions to `bus`.
- **A departure board.** A stop, or a route near him with no destination, gives the next buses at
  that stop. Each has its route, headsign and time, live or scheduled (ADR 0342), with both
  directions when he gave no destination.
- **A checked start.** A said ride compares his fix and each bus at one moment: the bus's position
  read at the fix's time, or his fix nearest the bus's report. Of the buses near him, a trip whose
  remaining stops include his stop wins; his stop comes from his pin, or from where he said he is
  going. When buses of different trips are still about equally near, he is asked by headsign.
- **Headsign.** Every ride answer names the bus by its route and headsign.
- **His stop.** `get_off` looks only at the stops the bus has not reached yet. A stop that the
  followed trip does not serve, but another trip of the same route near him does, means the wrong
  trip was matched. When exactly one bus of such a trip is near his fix, the ride moves to it;
  otherwise the answer says which way the followed bus is going.
- **Asked while riding.** A times question whose destination is the ride's stop or destination is
  answered from the ride: the arrival at his stop and the walk from it.

## Alternatives rejected

- **Keep the nearest bus within 300 m, and ask when two are close.** On 10-17 the wrong trip was
  not a close call by distance. Distance alone never sees direction.
- **Ask him which way every time he boards.** He has already said his stop or pinned a trip, and
  the timetable shows which trips serve that stop.
- **Answer "nearest stop" from Google Places.** A Places result has no BC Transit stop id and no
  routes, so neither the board nor the ride can use it, and it is a paid request for what the free
  timetable holds.
- **Keep 800 m (today).** That is about a ten-minute walk. At 300 m, with the distance said, he
  hears when a stop is far from where he meant.

## Consequences

- A ride can move to another trip on his word. If the move is wrong, it is put right only by him
  naming the stop again or ending the ride.
- A board's times for trips the realtime feed does not cover yet are scheduled.
- A stop name he says that shares no word with BC Transit's spelling is not found by name; he has
  to give a place or an address instead.
- The 300 m limit can refuse a stop he really uses, such as one at the end of a long block. He can
  still name the stop itself.
