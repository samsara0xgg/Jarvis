# ADR 0143 — Echo cancellation follows the open microphone

**Status:** Superseded-by-0191
**Date:** 2026-10-03
**Supersedes:** none

## Context

- Over the laptop speakers a microphone hears Jarvis; WebRTC AEC3 (`jarvis/surface/voice_aec.py`)
  subtracts what the player rendered. The reSpeaker XVF3800 cancels its own echo on channel 0
  (ADR 0103), so software cancelling a second time can only damage Allen's speech.
- The switch was a Settings key (`mac_aec`, ADR 0052) that applied at the next boot, while the
  microphone changes at runtime (unplugged, picked on the page, system default moved; ADR 0054).
  The canceller and the player's tap existed only when the key was on, so the setting and the
  microphone could disagree for a whole session, and a flip needed a restart.
- The native player taps what it renders (ADR 0129): 192 KB/s of float32 mono at 48 kHz over the
  helper's pipe, and the Python player calls the tap once per output callback.

## Decision

Build the echo canceller and give the player its tap whenever the voice ingress exists, and
decide at each microphone open from the opened device's name: a name containing `reSpeaker`
(any case) passes the microphone through untouched and feeds nothing to WebRTC; any other
microphone is cancelled. One log line per open says which device and which.

- **No Settings key.** `mac_aec` is removed from the page and from `jarvis/runtime/settings.py`;
  an old saved `mac_aec` in `settings.json` is ignored like any unknown key.
- **Debug override.** `realtime.single_audio_ingress.echo_cancellation`: `auto` (default) as
  above, `true` cancels every microphone, `false` builds no canceller and no tap.
- **Channel selection is unchanged**: the canceller cleans the same mono stream it did before.

## Alternatives rejected

- **Keep the switch and show a hint** — the choice is a function of the device, so a switch can
  only be wrong; it was wrong in practice (the page default was off while the laptop microphone
  was the usual input) and applied only at boot.
- **Rebuild the player when the microphone changes** — `refresh_devices` already stops and
  reopens both streams; a rebuild adds a second place that must agree with the first, and
  the tap's cost (192 KB/s, one block copy per callback) is below what the ingress already
  spends per frame.
- **Detect the board by USB vendor id** — CoreAudio names are what ADR 0054's device list and the
  Settings page already carry; an id needs a second enumeration path for the same device.

## Consequences

- A microphone the name test does not recognise but that cancels its own echo is cancelled twice;
  any other board with its own AEC needs its name added to the test.
- With the reSpeaker open the tap still crosses the helper's pipe (the canceller drops it on
  arrival); only a restart with `echo_cancellation: false` removes that traffic.
- Echo suppression on real speakers with this path is unmeasured; confirm with
  `echo_diagnostics` in a live run.
