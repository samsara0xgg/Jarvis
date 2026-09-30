# ADR 0093 — A night run keeps the Mac awake until its deadline and while agents work

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- Allen, 2026-09-29: "I'm going to sleep, keep it running." Jarvis should
  remember brightness and volume, turn them down or the screen off, and keep
  the system running; the first version runs for a fixed two hours. When the
  work ends in the night, only the keep-awake is let go: the Mac stays quiet
  and dark, and brightness, volume and the result come back when he does.
- Allen's answers the same day: the screen fully off is fine as long as the
  background keeps running; an unlock before 06:00 is a look, not getting up,
  and the screen goes dark again; 06:00 or later, or pressing End, is getting
  up (the time can be changed); the morning result is a card, never speech;
  a session waiting for his approval counts as stopped.
- Allen, 2026-09-30, after the timer shipped: the plan was to watch the work
  that has not finished, not a fixed time. Keep the timer as a floor (兜底)
  and also follow the agents; with a few nights of data, if watching proves
  enough, the floor may go to save power. He reviewed the cards for the night
  and the morning in two looks, a list and a star-trail dial, and kept both.
- Where the daemon can see agent sessions: the agent host lists the Agents
  window's sessions with their state (working, compacting, waiting, idle,
  failed) and background tasks, and answers the daemon's own token
  (`docs/spec.html#local-endpoints`). The daemon's boards list Claude Code
  sessions from `claude agents --json` when the owner turned reading on
  (ADR 0046, off by default) and Codex sessions from Codex's hooks. A
  terminal session with reading off, a download or a build is on none of
  them.
- ADR-0009 D3: the power observer never vetoes a sleep. Nothing else in
  Jarvis keeps the Mac awake.
- macOS: a `PreventUserIdleSystemSleep` assertion stops idle sleep only; the
  display still sleeps, and a closed lid (no external display), a low battery
  or the owner's own Sleep still put the Mac to sleep. An assertion belongs to
  its process and ends with it, and `IOPMAssertionCreateWithDescription` takes
  a timeout that powerd enforces on its own.
- Claude Code's own keep-awake relaunches `caffeinate -i -t 300` every 240 s,
  killing the old one first; once the display is off the Mac falls asleep in
  that gap (anthropics/claude-code#81832), which is the failure Allen is
  guarding against.
- Apple Silicon has no public brightness call; the private DisplayServices
  framework sets the built-in panel only. AppleScript's volume calls, which
  the wake ducker uses (ADR-0005 §4.2), read a Multi-Output Device as
  `missing value`: an aggregate has no level or mute of its own, only its
  sub-devices have one, through CoreAudio (the owner's Mac, 2026-09-29).
- The daemon runs inside the owner's login session, so it can read the lock
  state and the time since the last keyboard or mouse input itself; the
  companion may not be running, and a run started by voice must still end.

## Decision

While a night run the owner started lasts, the daemon holds one named
`PreventUserIdleSystemSleep` assertion at least until the run's deadline, and
past it for as long as an agent session it can see is working; nothing else
in Jarvis ever keeps the Mac awake.

Limits: past the deadline the hold ends once no session it can see has worked
for three minutes, and twelve hours after the start in any case; a session
waiting for the owner counts as stopped, one with a background task running
as working. When no list of sessions can be read, the deadline alone decides.
The assertion's OS timeout is the deadline; to hold past it, it is renewed
fifteen minutes at a time, the new assertion taken before the old one goes. Each run
records what it saw in the event log, so the morning can tell when the
deadline and when the sessions would have let the Mac go. The run writes the
brightness and sound it found before it changes them, then dims the built-in
panel, mutes each device behind the default output (each sub-device of an
aggregate) and puts the display to sleep. It puts back only what is still as
it left it, and only when the owner returns (input on an unlocked session at
or after the morning time, or End). It never blocks a lid-close, low-battery
or requested sleep, and the power observer still never vetoes one.

## Alternatives rejected

- **Only the sessions, no deadline floor** — work the daemon cannot see (a
  terminal session with reading off, a download, a build) would lose the Mac
  three minutes after the start; Allen asked to keep the floor until the
  recorded nights show how often it was the only thing holding.
- **One hold whose OS timeout is the twelve-hour cap** — a daemon that hangs
  while its process lives keeps the Mac up to the cap; renewing fifteen
  minutes at a time bounds that to fifteen minutes.
- **A relay of short holds, each killed before the next starts** (Claude
  Code's approach) — the gap lets a Mac whose display is off sleep mid-run
  (#81832); taking the new assertion before releasing the old leaves none.
- **A `caffeinate -i -t N -w <pid>` child** — its assertion is named
  "caffeinate command-line tool" in `pmset -g assertions`, the same name as
  Claude Code's relays, so a morning check cannot tell whose hold kept the Mac
  up; it is also one more process to supervise.
- **Watch only the Agents window's sessions** — Allen's agents also run in
  terminals and in Codex, and the daemon's boards already know those.
- **`sudo pmset -a disablesleep 1`** — needs root, also defeats lid-close
  sleep, and stays set after Jarvis dies.
- **The companion holds it with Electron's `powerSaveBlocker`** — it takes no
  timeout, and a run started by voice would depend on the companion window
  being up.

## Consequences

Work the daemon cannot see is covered only until the deadline, and a session
stuck working holds the Mac until the cap. While a run holds, the daemon reads
the agent host's session list every fifteen seconds; opening its event stream
also makes the host read the daemon's session marks again. A laptop with its
lid closed and no external display sleeps anyway; the bedtime card says so.
Brightness is only the built-in panel's, and a panel with auto-brightness may
be moved by the OS, in which case it is left as it is. A device the owner
unmuted in the night is left as it is; one the run muted is unmuted at the
return even when the owner has switched to another output. An output with no
mute, such as a display's own speakers, stays audible, and the log says so.
Jarvis's own speech is silent while the output is muted, and the companion
holds its notices for the whole run. A return is noticed by polling the lock
state and the input idle time every two seconds; if the OS answers neither,
the run ends only by the button or by words.
