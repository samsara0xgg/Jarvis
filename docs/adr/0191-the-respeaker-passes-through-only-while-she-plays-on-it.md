# ADR 0191 — The reSpeaker passes through only while she plays on it

**Status:** Accepted
**Date:** 2026-10-07
**Supersedes:** 0143

## Context

- Over the laptop speakers a microphone hears Jarvis; WebRTC AEC3 (`jarvis/surface/voice_aec.py`)
  subtracts what the player rendered. The reSpeaker XVF3800 cancels echo on channel 0 (ADR 0103),
  but its reference is its own USB output: it can only remove what is played through the board.
- ADR 0143 decided from the microphone's name alone. On 2026-10-07 the input moved from the
  MacBook microphone to the reSpeaker while she kept playing on the MacBook Pro Speakers (the
  system default): neither the board nor WebRTC removed her voice. In the 17:16-18:02 live run
  9 of her own lines were judged echo, 12 barge-ins were confirmed on her voice, every reply that
  drew one ran 0.9-1.7 s longer than its audio (playback held while the barge-in was judged), and
  7 of 14 replies were cut off. Playback reported 0 underflows and 0 starvation gaps throughout.
- The microphone, the picked speaker (`realtime.output_device`, ADR 0052) and the system default
  all change at runtime (ADR 0054); every change reopens the microphone.
- Cancelling a microphone whose board already cancelled can only damage Allen's speech.

## Decision

Pass the reSpeaker through untouched only while the speaker she plays on is the reSpeaker too;
cancel every other pair, at each microphone open. The speaker is the picked one, else the
system default output; when neither can be read, the microphone's name decides alone, as before.

- The debug override `realtime.single_audio_ingress.echo_cancellation` (`auto`, `true`, `false`)
  and its meanings are unchanged; `auto` is this rule.
- One log line per open names the microphone, the speaker and the choice.

## Alternatives rejected

- **Keep the name test and route her through the board** (a Multi-Output Device with the
  reSpeaker in it, or a speaker on its jack) — correct only while the routing holds; the routing
  was lost without any change to Jarvis and the 10-07 run shows what that costs.
- **Always cancel** (`echo_cancellation: true`) — a board that plays her and cancels its own
  echo would be cancelled twice, which ADR 0143 rejected as damaging Allen's speech.
- **Read the members of a Multi-Output Device** — a second CoreAudio enumeration path for a
  setup that is not in use; the name test on the device she plays on covers the reSpeaker case.

## Consequences

- A Multi-Output Device that contains the reSpeaker is cancelled twice: its name is not the
  board's.
- A speaker change that does not reopen the microphone (the system default moved with no device
  refresh and the route observer off) is followed only at the next open.
- Final ASR may still hear channel 1, the board's beam, which no canceller cleans (ADR 0133).
