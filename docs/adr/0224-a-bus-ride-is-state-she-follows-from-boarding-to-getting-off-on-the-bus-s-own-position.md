# ADR 0224 — A bus ride is state she follows from boarding to getting off, on the bus's own position

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- A live case on 2026-10-11: he told her he had boarded. Minutes later he asked how long the
  ride had left, and she answered with the times of a different bus on the same route, because
  nothing recorded which bus he was on. He then asked for three things:
  - that she ask him on her own whether he has boarded;
  - that she know where he is during the ride and when he gets off;
  - that the ride show on the star chart's ring, with progress like Google Maps (which stop,
    how long until he gets off).
- On 2026-10-11 Allen asked that every decision be the long-term optimum, not a patch, and
  approved this design (「可以」) in the phone planning thread.
- What exists:
  - A plan: the departure pin (ADR 0200, 0204, 0205). Up to three trips, each with its route,
    board stop and times, refreshed every 30 s from BC Transit's TripUpdates feed (`BusLive`).
  - A route tool: `transit`, one Google Routes call (ADR 0189, 0221). Google gives timetables,
    not live vehicles.
  - Nothing persists "he is on this bus now". Turn context has no trip (ADR 0217 carries the
    device and the phone's last place only).
- BC Transit publishes Victoria (operator 48) openly, with no key:
  - static GTFS, about 16 MB, already cached daily;
  - TripUpdates, about 362 KB;
  - VehiclePositions (`vehicleupdates.pb`), about 12.7 KB, with a header timestamp advancing
    live on every request (measured 2026-10-11).

  The licence is revocable, requires attribution, and comes with no warranty and no SLA.
- The phone already reports a fix at most every 30 s and 50 m while it moves, plus motion
  activity including `automotive` (`LocationCollector`, ADR 0197). The brain cannot command the
  phone:
  - there is no silent push and no `remote-notification` mode;
  - Apple does not guarantee silent pushes and limits them to 2-3 per hour;
  - continuous background location needs a session the app starts in the foreground.
- APNs reaches the phone with alert pushes and Live Activity updates (ADR 0210). ActivityKit can
  start a Live Activity by push (push-to-start token) and update it by push. Priority-5 updates
  do not count against the hourly budget. An activity lasts at most 8 h.
- The day line (ADR 0199) already draws a bus ride as a generic `move` (automotive), "在路上".

## Decision

A ride is event-log state: started when he confirms or says he has boarded, ended when he leaves
the vehicle. While it lasts, she reads where he is from the bus's own live position, not from
the phone. Its limits:

- **The ride.** One active ride at a time, folded from `ride.*` events like the pin and
  reminders. It holds:
  - the GTFS trip and vehicle, the route, the board stop and, when known, the stop to get off;
  - how it started: tapped, said, or matched from a pin.

  It is never inferred into existence without his tap or his words.
- **Watching for boarding.** A watch runs only while it has reason to:
  - a pinned trip is within 10 minutes of leaving;
  - or he said he is heading out, in a turn that names a destination.

  While it runs, the brain polls VehiclePositions every 15 s. When two consecutive phone fixes
  are within 60 m of the same vehicle on the watched route (or on any route, once he has said
  he is leaving), and they move with it, one actionable push asks 「上 N 路了吗？」 with
  上车了 / 不是 buttons. Each trip gets at most one push, and nothing is sent once he has said
  so himself.
- **Saying it.** 「上车咯」 in a turn starts the ride through a tool, which matches the vehicle
  nearest his last fix on a route he mentioned or pinned. If the match is ambiguous, she asks
  which bus instead of guessing.
- **During the ride.** The vehicle's feed position and TripUpdates give the next stop, the stops
  left, and the arrival time at his stop. Every turn's state block carries that line, so
  「还有多久」 is answered about this bus.
  - The stop to get off comes from the pin, or from his destination (a named place means the
    trip's stop nearest it). If it is still unknown, she asks once.
  - She warns him one stop before.
- **Ending.** The ride ends when one of these happens:
  - his fixes leave the vehicle by more than 150 m twice;
  - the trip passes his stop and his motion turns to walking or still;
  - he says so;
  - or 15 minutes pass after the trip's scheduled end.

  The ride is then a finished item with its route and stops.
- **The phone.** It draws the ride on the ring as its own segment (route number, board and
  alight stops). The ride replaces the generic `move` for that span. A Live Activity is started
  by push-to-start when the ride starts and updated by push on each change: route, next stop,
  stops left, minutes to his stop. Updates go at priority 5, and at priority 10 only for the
  one-stop warning. The activity ends with the ride.
- **No new egress.** The feeds are fetched and nothing about him is sent out. BC Transit is
  credited where ride data is shown.

## Alternatives rejected

- **Track him with the phone's own continuous location.** That needs a background session
  the app must start in the foreground, so a push cannot begin it. It also costs battery
  (Transit's app quotes about 5% for 20 minutes). Once he is on a known vehicle, the bus reports
  its own position anyway. The phone's existing 30 s fixes are enough to see him board and leave.
- **Start rides automatically, with no tap.** A car beside a bus, or a bus he walks next to,
  moves the same way for a while. He asked to be asked (「如果我点上车的话」), and a wrong ride
  would put a false stretch on the timeline that he never sees being made.
- **Motion activity alone (`automotive`).** It cannot tell a bus from a car, or which bus, so
  there are no stops and no arrival time. The day line already has exactly that today.
- **Google Routes for progress.** Its transit details have timetables, stops and fares, but no
  vehicle position and no per-trip live prediction.
- **Extend the departure pin instead of a new ride.** A ride often has no pin: last night he
  boarded without pinning anything. A pin is a plan that may never be ridden, and a ride is
  something that happened, which the timeline keeps. The ride links the pin's trip when there
  is one.

## Consequences

- Rides depend on BC Transit's open feeds, which have no SLA and a revocable licence. When a
  feed fails, the ride falls back to the timetable and says so. Outside Victoria there is no
  vehicle data, so there is no detection; only a ride he declares, on timetable times.
- A 12.7 KB fetch every 15 s while a watch or ride is active, about 50 KB a minute.
- The boarding push needs a new category whose payload carries the ride id. The current
  button path finds its card by body text, which is not enough here.
- Push-to-start needs a new token registered from the phone and a new activity type. Live
  Activity updates are budgeted by iOS, and a user can turn frequent updates off.
- The 30 s, 50 m phone cadence can miss a very short ride, and can detect leaving the bus up to
  a minute late.
- The day line gets a new item kind, which the phone must draw.
