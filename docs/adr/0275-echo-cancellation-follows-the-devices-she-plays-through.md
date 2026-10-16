# ADR 0275 — Echo cancellation follows the devices she plays through

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** 0191

## Context

- Allen's Mac setup since 2026-09-29: input the reSpeaker XVF3800, output "Multi-Output Device 2"
  (MacBook Pro Speakers plus the reSpeaker). The board's hardware AEC (ADR 0103) references only
  what goes out its own USB output, so while the system default output is that Multi-Output,
  everything the Mac plays (music, video, her voice) is cancelled by the board.
- The system default output drifted back to the MacBook Pro Speakers on 2026-10-07 and again
  later, with no change to Jarvis. She keeps playing on the picked speaker (`realtime.output_device`,
  ADR 0052), so her voice is still cancelled, but music from other apps is not and leaks into his speech.
- ADR 0191 decides "the board plays" from the output's name containing "respeaker". "Multi-Output
  Device 2" does not, so WebRTC AEC ran on top of the board's AEC and damaged his speech. It
  rejected reading a Multi-Output's members as "a setup that is not in use"; it is the setup in use.
- Allen's rule for his three setups: reSpeaker with the Multi-Output is cleaned by the board; the
  system speakers with the MacBook microphone are cancelled in software; AirPods need none.
- Over a private output (Bluetooth, the built-in headphone jack, a headphone-named device,
  ADR 0155) the microphone cannot hear her.
- Route changes reopen the microphone (ADR 0054) and each open re-decides; the microphone, the
  picked speaker and the default all change at runtime.

## Decision

At each microphone open, read where she plays (the picked speaker, else the system default
output) as the devices it plays through (an aggregate or multi-output is its active sub-devices,
any other device itself), cancel in software only if she plays on no private output and the
reSpeaker is not both the microphone and one of those devices; when the microphone that opens
is the reSpeaker and the default output is the built-in speakers, first make the default output
a multi-output whose active sub-devices include the reSpeaker, if one exists.

- Output unreadable: the name test of ADR 0191 stands (the speaker's name contains "respeaker").
- The pull touches nothing else: never a default that is headphones, Bluetooth or any other
  device, and never when no such multi-output exists.
- The pull runs before the output route is read, so the route observer's baseline and the
  cancellation choice both see the result and the pull cannot reopen the microphone itself.
- The debug override `realtime.single_audio_ingress.echo_cancellation` (`auto`, `true`, `false`)
  keeps its meaning; `auto` is this rule. One log line per open names the microphone, the
  speaker, the devices read and the choice; one per pull.

## Alternatives rejected

- **Keep the name test and ask Allen to repair the routing by hand** — the routing was lost on
  2026-10-07 and again afterwards without any change to Jarvis; two drifts in a week show a
  manual fix does not hold.
- **Always cancel in software** (`echo_cancellation: true`) — a board that cancels what plays
  through it would be cancelled twice, which ADR 0143 rejected as damaging his speech.
- **Pull the default output at every poll** — the system output is Allen's; moving it without a
  reSpeaker microphone opening would fight him whenever he picks a speaker on purpose.

## Consequences

- A microphone open with the reSpeaker moves the system default output when it is the built-in
  speakers and a reSpeaker multi-output exists: his choice of the laptop speakers while the
  reSpeaker is the microphone is undone at the next open.
- Music from other apps is cancelled by the board only while the Multi-Output is the default.
- A player already open on the old default is not moved by the pull; it follows the picked speaker.
- Final ASR may still hear channel 1, the board's beam, which no canceller cleans (ADR 0133).
