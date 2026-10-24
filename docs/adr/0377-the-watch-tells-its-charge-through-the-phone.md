# ADR 0377 — The watch tells its charge through the phone

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** none

## Context

- On 2026-10-23 yilun chose 「做」 for knowing when his watch is worn and when it charges.
  Today the only trace of the watch taken off is a gap in its heart rate (Health, ADR 0293),
  and a gap looks the same whether the watch charges or lies on a table.
- watchOS has no public API that says whether the watch is on the wrist (Apple DTS, developer
  forums thread 811405). SensorKit's wrist detection needs a research entitlement Apple grants
  to studies, and delivers its samples 24 hours late.
- `WKInterfaceDevice` gives the watch app the battery level and whether it charges, is full or
  unplugged, whenever the app runs, a background refresh included. watchOS grants an app about
  four background refreshes an hour while one of its complications is on the active face, and
  fewer without one.
- `WKExtendedRuntimeSession` keeps an app running only for self care (10 minutes),
  mindfulness and physical therapy (an hour each) or a smart alarm; none runs all day. A
  workout session keeps the heart sensor and the app running as long as it lasts.
- iOS has no public API that reads a paired watch's battery from the phone.
- The watch reaches the brain only through the phone and holds no token (ADR 0337).
  `transferUserInfo` queues a dictionary and delivers the queue in order when the other app
  next runs, across kills of either app; `sendMessage` reaches only an app reachable now.
- The phone reports its own charge as `phone.state_observed` with the signal `battery`: the
  percent as the value and `charging`, `full` or `unplugged` as the detail (ADR 0292).

## Decision

The watch app reads its own charge each time it runs, and in a background refresh it asks for
every 15 minutes, and queues every reading to the phone; the phone reports each one as
`phone.state_observed` with the signal `watch_battery`, carrying the value and detail of
`battery` and stamped with the moment the watch read it.

- Every reading goes, unchanged ones too: a run of the same reading is what says the watch
  stayed on its charger, or stayed unplugged.
- Nothing decides at ingestion whether the watch was worn. A reader puts the charging state
  beside the heart-rate gaps.
- The route of ADR 0337 stands: the reading reaches the brain only as a frame the phone sends
  over its own pairing.

## Alternatives rejected

- **SensorKit wrist detection** — the entitlement is granted only to research studies, and
  its samples arrive 24 hours after the fact.
- **Heart-rate gaps alone** — the watch measures heart rate in the background only every few
  minutes, so a gap of minutes is normal while worn, and a long gap is the same on the
  charger and on a table.
- **Keep the watch app running to watch the charge live** — no extended runtime session
  lasts beyond an hour, and a workout session kept open all day would run the heart sensor
  at its workout rate all day, for a reading that changes a few times an hour.
- **Send each reading with `sendMessage`** — a reading taken while the phone is out of reach
  would be lost; `transferUserInfo` keeps it until the phone app runs.
- **Send only changes** — a reader could then not tell the watch on its charger all night
  from a watch app that stopped being woken.

## Consequences

- The moment the watch went on or came off its charger is known only to within the gap
  between refreshes: about 15 minutes with a Jarvis complication on the active face, and
  unbounded without one. The watch app must have been opened once after install before
  watchOS refreshes it.
- A reading reaches the brain when the phone app next runs; its time is the watch's.
- The event log grows by about a hundred small frames a day.
