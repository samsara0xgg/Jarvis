# ADR 0198 — On a brain, "here" is the phone's latest report

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- `where_am_i` and the `here` of `transit` read the Mac's CoreLocation on demand (ADR 0194).
  On a brain there is no Mac, so both are absent.
- Allen carries his phone everywhere. The MacBook travels with him too, but it is often closed
  in a bag.
- Battery is a hard gate for the phone (ADR 0197), which rules out continuous GPS. iOS visit and
  significant-change monitoring wake the app on a stay or on a move of roughly 500 m, and the
  app reports a fix at those wakes.
  - Between two reports he has moved less than about 500 m.
  - So an old report is still where he is, unless reporting stopped.
- ADR 0194 refused to cache the Mac's fix because an old fix would put a bus trip at the wrong
  stop. A Mac fix ages whenever he moves; a phone report is replaced when he moves.
- The brain cannot ask the phone for a fresh fix (ADR 0197).

## Decision

On a brain, where Allen is is the most recent place or location fix any paired phone reported,
read from the event log when asked and returned with its age.

Its limits:

- **Nothing is cached.** The log is read at each call.
- **The answer says how old it is.**
  - It carries the fix's age, its accuracy, and that it is the phone's last report.
  - There is no age cutoff.
  - The model is told that the phone reports when he moves, so an old report means he has
    not moved far since.
- **A visit counts at its latest known time.**
  - Its arrival places him there from the arrival time.
  - Its departure places him leaving there at the departure time.
- **No report means no location.** `here` is then a tool error, as it is off macOS today, and the
  model asks where he is.
- **A Mac running alone (`role: all`) is unchanged.** It reads its own location as in ADR 0194.

## Alternatives rejected

- **Ask the terminal Mac for a live read.** When he is out, the Mac is closed and its terminal
  is disconnected. When the Mac is open he has both devices, and the phone's report is within
  about 500 m.
- **Wake the phone for a fresh fix with a silent push.** The push arrives at the system's
  discretion and takes seconds when it does, so the call would hang on it.
- **Drop reports older than some hours.** A phone that stays put sends nothing, so a night at
  home would read as "unknown" by morning.

## Consequences

- If the phone stops reporting (app force-quit, location permission lowered to "while using"),
  "here" goes stale, and only its age shows it.
- The accuracy is the phone's. A significant-change fix is often off by hundreds of meters, so
  a bus trip from "here" can start at a stop a few hundred meters away.
