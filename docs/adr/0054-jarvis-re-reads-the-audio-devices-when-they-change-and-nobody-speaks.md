# ADR 0054 — Jarvis re-reads the audio devices when they change and nobody speaks

**Status:** Accepted
**Date:** 2026-09-26 (amended 2026-10-03: retry a microphone that is down, and an absent pick follows the default)
**Supersedes:** ADR 0052's "a saved change applies on the next restart" for the microphone and the speaker only; the rest of ADR 0052 stands.

## Context

- 2026-09-26: Allen unplugged the reSpeaker overnight and plugged it back in;
  Jarvis stayed deaf until a restart. The daemon log at 00:17:50 shows three
  reopens inside half a second, then `reopen_budget_exhausted`, then nothing.
- PortAudio (V19.7.0 under sounddevice 0.5.6) lists the devices and the
  system defaults once, when it initialises. A device plugged in, or made the
  default, afterwards is invisible to every open until PortAudio terminates
  and initialises again. For the same reason ADR 0052's context line that the
  input "reopens when that default changes" does not hold: the default it
  compares against is PortAudio's cached one.
- Terminating PortAudio frees every stream the process still has open under
  its owner: the microphone, the answer player, and a GPT-Live session's
  player. All of them must be closed first and reopened after.
- Closing the player mid-answer cuts her off. A gap in the microphone
  mid-sentence fails Allen's capture. ADR 0053 already parks answers that
  would start while he talks.
- CoreAudio reports the device list and the default input and output
  directly, in microseconds, so polling once a second costs nothing. The
  list itself is noisy: Allen's iPhone microphone (Continuity) left and
  rejoined it twice in 80 s on 2026-09-26 with nothing touched.
  Measured on Allen's Mac (reSpeaker in, Multi-Output Device out), a full
  re-read took 0.31–0.33 s over three rounds: 0.24 s closing, 0.007 s
  initialising PortAudio, 0.07 s reopening. The microphone delivered 29
  callbacks in the second after each round.
- 2026-10-03: the microphone stayed shut from 23:49 until a restart at
  10:16. A device change at 23:49:25 re-read the devices, but the
  reSpeaker, though listed, failed to open, and a failed open after a
  re-read counted as handled, so nothing tried again. At the 09:48:52 wake
  it failed again, the wake parked input and output both, and a parked
  microphone was never re-read. The same failed open after a re-read is in
  the log on 2026-09-29, 09-30 and 10-02.
- Allen asked on 2026-09-26 for all of these: reconnect by itself, re-read
  only when a device really changed, wait until she has finished speaking,
  reopen her speech channels (GPT-Live's included), and let the Settings
  page's microphone and speaker apply without a restart. The switch is for
  Jarvis only, not the whole Mac.
- 2026-10-03: with the reSpeaker unplugged Jarvis heard nothing. The pick in
  `~/.jarvis/settings.json` named a device that was gone, so the open failed
  and a daemon start without it ran with no microphone, and no device watch,
  until a restart. Allen expects the Mac's default input and output meanwhile
  and his pick again when it returns.

## Decision

Once a second, ask CoreAudio which device Jarvis should be on for input and
for output: the system default where it follows the default, else the
picked device, if present. When either differs from the last re-read, when
the microphone is newly lost, or when the Settings page picks a microphone
or speaker, wait until nothing is playing, Allen is not mid-sentence and
GPT-Live is silent. Then park new answers, close every stream, initialise
PortAudio again, and reopen the microphone and the speakers on those
devices. A picked device that is absent no longer stays closed (amended
2026-10-03): the microphone and the speaker open on the system default
instead, and the next re-read, which the pick's return triggers, moves them
back. The microphone then opens on channel 0 only, since the wake channel is
the reSpeaker's raw beam. A start without the picked microphone opens the
default, so the voice session and this watch exist. A
microphone that is down while its device is there, lost or parked by a wake
that failed, is re-read again after 2, 5 and 15 s, then every 30 s, until it
opens; a parked one wakes again after the re-read. Nothing is re-read from
the system's sleep notice until a wake has run.
The Settings page lists the devices CoreAudio has now. Jarvis never changes
the Mac's own default devices.

## Alternatives rejected

- **Leave an absent pick closed** (the original decision) — a pick that is
  unplugged left Jarvis deaf and mute until the device came back or a restart.
- **Restart the daemon on a device change** — it ends a GPT-Live call and
  resets conversation mode and the mute switches, which are not persisted.
  A daemon start also takes about 3 s (23:50:39 to 23:50:42 on 2026-09-25),
  against 0.33 s for a re-read.
- **Keep retrying the reopen without initialising PortAudio again** — every
  retry opens against the stale list. The 2026-09-26 log shows the same
  failure three times in a row.
- **Re-read on a fixed timer whether or not anything changed** — every
  re-read closes the microphone and the speakers, so a timer interrupts
  listening for no reason.
- **Re-read on any change in the device list** — the iPhone microphone alone
  changed the list twice in 80 s. A test daemon re-read once without any
  pick at 10:58:21, and each re-read leaves the microphone deaf for a third
  of a second, cutting the start of a sentence said then.
- **Switch the Mac's default input and output from Jarvis** — Allen chose
  Jarvis-only on 2026-09-26. Changing the system default moves every other
  app too. Moving the output off the Multi-Output Device also takes away the
  reSpeaker's echo reference.

## Consequences

Each re-read closes the microphone and the speakers for about a third of a
second, and a wake word said in that gap is missed. An answer that arrives
during a re-read starts that much later. A device that is unplugged and
plugged back inside one poll, while its stream still reports no fault, goes
unnoticed until something else changes. PortAudio silences stderr
while it initialises, so any daemon log line written in those milliseconds
is lost. The re-read uses sounddevice's private `_terminate` and
`_initialize`, so a sounddevice upgrade has to be checked against them. A
coreaudiod restart that keeps the same device ids is recovered only through
the lost-microphone trigger. A microphone that never opens though its device
is listed closes the speakers for a third of a second every 30 s while
nobody speaks.
A built-in microphone and speakers have no echo reference, so with
software echo cancellation off she may hear her own voice until the pick is
back; this amendment does not address that.
