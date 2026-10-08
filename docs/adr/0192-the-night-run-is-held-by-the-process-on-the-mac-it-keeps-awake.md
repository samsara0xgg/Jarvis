# ADR 0192 — The night run is held by the process on the Mac it keeps awake

**Status:** Accepted
**Date:** 2026-10-07
**Supersedes:** 0093

## Context

- ADR 0093 gave the night run to the daemon: it holds the keep-awake assertion, dims and
  mutes the Mac, looks at the agent sessions it can see, and reads the lock state and input
  idle time itself because it runs in the owner's login session.
- ADR 0170 made the brain a host with no Mac of its own, so the brain boots with no night run
  and without the two night tools. Nothing on a brain setup could start a night.
- On 2026-10-07 (17:16-18:02, a terminal against the Pi) the companion asked
  `GET /inherent/night` every 1.5 s, 1849 times, and the brain answered 404 every time. The
  card was absent and the model had no way to say goodnight.
- The run's subject is the device: the assertion, the panel, the output, the lock state and
  the agent host all belong to the Mac, and a hold with no process on that Mac to end it
  would leave the display dark. A relay round trip is about 80 ms (ADR 0183).

## Decision

The process that runs on the Mac the owner sleeps beside holds the night run: the daemon on
one machine, the terminal on a device of a brain.

Everything ADR 0093 decided about the run holds, with that process as the holder:

- While a run the owner started lasts, it holds one named `PreventUserIdleSystemSleep`
  assertion at least until the deadline, and past it while a session it can see is working;
  the hold ends once none has worked for three minutes, and twelve hours after the start in
  any case.
- It writes down the brightness and sound it found, dims the built-in panel, mutes each
  device behind the default output and puts the display to sleep; it puts back only what is
  still as it left it, and only when the owner returns or says so.
- It never blocks a lid-close, low-battery or requested sleep.

What the terminal adds:

- It keeps the run's `night.*` events in `terminal/night-events.db` under its runtime root,
  the only log it owns, and picks an open run up from them when it starts.
- It answers `GET` and `POST /inherent/night` itself, as a device route (ADR 0183), and the
  brain forwards nothing for them.
- The brain's model keeps `start_night_run` and `end_night_run` on its menu. Their handlers
  are proxies that run on the connected terminal, which declares them; with none connected
  the tool answers that the device is not there.
- It reads Claude Code's sessions and the agent host on its own Mac. Codex's board is filled
  by hooks that post to the brain, so a terminal's run does not see Codex.

## Alternatives rejected

- **The brain holds the run and sends the terminal each step** (hold, dim, mute, sleep) —
  the lock state and input idle time are readable only on the Mac, so every look crosses the
  link, and a link that drops after the display sleeps leaves nobody to put brightness and
  sound back. The run has at least six switches and a look every 15 s.
- **The terminal runs it with no log** — ADR 0093 recovers an open run after a restart from
  its events and tells the morning from them; without a log a restart in the night forgets
  the hold and the saved brightness.
- **Keep the 404 and silence the companion** — leaves every brain setup without a night run,
  which is the failure this fixes.

## Consequences

- A terminal now owns one small log. The morning card's numbers for a Mac come from that
  Mac's file, and the brain's log has no `night.*` events.
- With two terminals connected, the one that connected last and declared the tool gets the
  call (`TerminalHub.call`); the companion of the other shows the night run of its own Mac.
- A terminal older than this change declares no night tools; the brain's tool says the
  device is not connected until that terminal restarts.
- The route list in ADR 0183's body is not complete; `DEVICE_ROUTES` owns it.
