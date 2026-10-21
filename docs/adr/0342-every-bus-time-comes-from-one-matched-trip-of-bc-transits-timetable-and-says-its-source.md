# ADR 0342 — Every bus time comes from one matched trip of BC Transit's timetable, and says its source

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0200, finding a pinned bus's trip by its route and the closeness of its time alone

## Context

- One bus can get two times in one answer. A bus he names takes BC Transit's live departure, cut
  to the minute (09:52:40 became 9:52). The listed options, the chat card and the pin keep
  Google's 9:53, and the pin's refresh rounds the live time to the nearest minute. Neither time
  says where it came from. A bus he did not name gets no live time at all.
- The live match takes any trip of the route that stops within 40 m of Google's stop, with a time
  within ten minutes. The two stops of a street, one per direction, are often within 40 m of each
  other, so the match can take the bus going the other way. Nothing checks the direction.
- BC Transit's static export (about 16 MB zipped) is the only source of its stops, trips and
  timetable. Today one trip's stops are read by a full pass over every stop time in the export,
  and the last 20 trips are kept. The service calendars are never read, so the program cannot say
  which trips run on a given day.
- Google's transit answer gives, for each bus leg, the route's short name, a headsign, the board
  and alight stops with their points, and the times. It gives no BC Transit trip or stop id.
- A bus must be reached before it leaves. A departure said later than the real one costs him the
  bus; said earlier, it costs him a minute. For an arrival it is the other way round.
- ADR 0261 already keeps a ride's two facts apart, each with its source: where the bus is, and when
  he arrives.

## Decision

Match every bus the program speaks of to one trip of BC Transit's timetable, and give it one time
from that trip, rounded one way and labelled live or scheduled.

- **One copy of the timetable.** Each export the runtime downloads is indexed once, off the request
  path, into one local store beside it: stops, routes, trips with their direction and headsign,
  stop times by stop and by trip, and the service calendars. Every reader of the timetable reads
  the store. A request waits for indexing only when no store exists yet.
- **One match.** A Google bus leg is matched to a trip of the same route on a service day that
  runs. The trip must stop within 40 m of Google's board stop and, later on the same trip, within
  40 m of Google's alight stop; that order is the direction. Of those trips, the match is the one
  whose departure at the board stop is closest to Google's, and no more than ten minutes from it. A
  leg with no such trip keeps Google's time, labelled scheduled.
- **One time.** A matched bus's time is the live prediction for that trip at that stop, read from
  one realtime snapshot shared by the whole answer; with no prediction it is the scheduled time.
  One function rounds departures down to the minute and arrivals up. Every surface uses it: the
  answer, the card, the pin and its refresh, and the ride.
- **One label.** Each time is said as live or scheduled. The verdict and spare minutes use that
  time.
- **The pin keeps its trip.** A pinned bus carries its matched trip id, and the refresh follows
  that trip. A trip the realtime feed stops covering keeps its last time and is marked stale, as
  an unmatched pin is today.

## Alternatives rejected

- **Prefer Google's time, and look up live only for a bus he names (today).** This is what put
  9:52 and 9:53 side by side in one answer, unlabelled.
- **Check the direction by comparing headsigns.** A headsign names where a trip ends, not whether
  it passes his stops. Two trips of one route in one direction can end at different places, and a
  feed without a headsign leaves nothing to compare. Stop order is in the timetable for every trip.
- **Keep reading the zip with a full pass per question.** A departure board needs every trip at a
  stop on a service day, so each board question would read every stop time and the calendars, and
  the 20-trip cache never holds a board. This choice is wrong if such a pass on the Pi takes under
  0.2 s.
- **Round every time to the nearest minute.** A departure at 09:52:40 said as 9:53 is 20 seconds
  after the bus leaves. A rule that errs in the costly direction half the time is the wrong rule.

## Consequences

- The program keeps a second copy of the timetable and rebuilds it with each download: disk space,
  and a build once a day on the Pi.
- An answer depends on the export agreeing with Google. A stop Google places more than 40 m from
  BC Transit's, or a trip Google has and the export lacks, keeps Google's time.
- A matched trip that the realtime feed does not cover is said as scheduled, even when Google had a
  live time for it.
- Rounding departures down can make a bus look a minute tighter than it is.
