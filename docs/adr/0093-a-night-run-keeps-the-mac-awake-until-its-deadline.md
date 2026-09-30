# ADR 0093 — A night run keeps the Mac awake until its deadline

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
  up (the time can be changed); the morning result is a card, never speech.
- ADR-0009 D3: the power observer never vetoes a sleep. Nothing in Jarvis
  keeps the Mac awake today.
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
`PreventUserIdleSystemSleep` assertion whose OS timeout is the run's deadline,
and nothing else in Jarvis ever keeps the Mac awake.

Limits: the run writes the brightness and sound it found to the event log
before it changes them, then dims the built-in panel, mutes each device
behind the default output (each sub-device of an aggregate) and puts the
display to sleep. At the deadline it releases only the assertion. It
puts back only what is still as it left it, and only when the owner returns
(input on an unlocked session at or after the morning time, or End). It never
blocks a lid-close, low-battery or requested sleep, and the power observer
still never vetoes one.

## Alternatives rejected

- **A relay of short holds renewed while the run lasts** (Claude Code's
  approach) — the kill-before-spawn gap lets a Mac whose display is off
  sleep mid-run (#81832); one hold with the whole duration has no gap.
- **A `caffeinate -i -t N -w <pid>` child** — its assertion is named
  "caffeinate command-line tool" in `pmset -g assertions`, the same name as
  Claude Code's relays, so a morning check cannot tell whose hold kept the Mac
  up; it is also one more process to supervise.
- **Hold until the agents are done, no deadline** — the daemon cannot see the
  sessions the Agents window hosts (ADR 0073), and a hold without a timeout
  outlives a hung daemon until a laptop's battery is empty.
- **`sudo pmset -a disablesleep 1`** — needs root, also defeats lid-close
  sleep, and stays set after Jarvis dies.
- **The companion holds it with Electron's `powerSaveBlocker`** — it takes no
  timeout, and a run started by voice would depend on the companion window
  being up.

## Consequences

A laptop with its lid closed and no external display sleeps anyway; the
bedtime card says so. Brightness is only the built-in panel's, and a panel
with auto-brightness may be moved by the OS, in which case it is left as it
is. A device the owner unmuted in the night is left as it is; one the run
muted is unmuted at the return even when the owner has switched to another
output. An output with no mute, such as a display's own speakers, stays
audible, and the log says so. Jarvis's own speech is silent while the output
is muted, and the companion holds its notices for the whole run. A return is
noticed by polling the lock state and the input idle time every two seconds;
if the OS answers neither, the run ends only by the button or by words. The
first version ends at a fixed time, so the Mac may sleep while an agent still
works or stay up after every agent has stopped, until completion detection
replaces the timer.