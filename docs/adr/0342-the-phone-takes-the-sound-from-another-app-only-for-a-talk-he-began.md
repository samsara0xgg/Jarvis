# ADR 0342 — The phone takes the sound from another app only for a talk he began

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- On 2026-10-21 he reported that the phone app keeps cutting his microphone out of a Discord call
  with his girlfriend at night, and that the call rarely survives switching to Jarvis. His rule:
  Jarvis never interrupts Discord, except while he is actually talking to her.
- iOS has one audio session per app and the last non-mixable activation wins: activating one
  interrupts every other app's non-mixable session (a voice call, music). An interrupted app
  goes on only when the one that took the sound deactivates with `notifyOthersOnDeactivation`,
  and some apps do not go on even then.
- The phone's voice (ADR 0209) needs `playAndRecord`/`voiceChat`, non-mixable: its echo
  canceller is the only one, and it must hear what she plays. It cannot run beside a call
  without taking it.
- The phone's own log (2026-10-20 18:48, 18:50, 21:19, 21:41; 10-21 00:41) shows the voice
  session started each time he brought the app on screen, with no tap: the in-app wake word
  (on for him) armed that non-mixable session on every foreground and after every talk. At 00:41
  Discord held the microphone, the start failed, and the session was left active.
- After a talk the voice also kept the session for a 10 s follow-up window, and with the day's
  recorder (ADR 0238, 0292) on it handed the session to the recorder instead of back to the
  interrupted app, deactivating without telling it.
- A call through CallKit shows in `CXCallObserver`; one that is not (Discord's voice channels
  are not always) shows only as another app's audio on (`isOtherAudioPlaying`,
  `secondaryAudioShouldBeSilencedHint`).

## Decision

The phone takes the audio session from another app's audio or call only for a talk he began
(a tap, the Action button, Siri, the widget or Live Activity's talk), and gives it straight back.

- The in-app wake word does not arm while a call or another app's audio is on; it arms again
  the next time the app comes on screen with nothing else sounding.
- A talk that took the sound from another app ends with her answer: no follow-up window, the
  session is deactivated with `notifyOthersOnDeactivation`, and the recorder waits for the other
  app as it does for a call. A failed start lets the session go the same way.
- His own 「等一下」 still holds the microphone open: he asked for it.

## Alternatives rejected

- **A mixable voice session during a call** — two voice-processing units at once: the call's
  echo canceller does not hear her and his voice reaches the call processed twice; on
  2026-10-12 a mixable recorder beside a Discord call already took his microphone from it.
- **Refuse to talk while another app's audio is on** — he wants to be able to ask her mid-call;
  his rule allows the interruption while he is talking to her.
- **Detect only calls (CallKit)** — a Discord voice channel need not go through CallKit, so the
  cut he reported would remain.
- **Have the recorder also refuse to start beside any other audio** — the recorder is mixable
  and runs beside music on purpose; `isOtherAudioPlaying` does not tell music from a call, so
  this would drop his recorded day whenever music plays. It is off now; its call handling stays.

## Consequences

- With music playing and the in-app wake word on, the wake word does not listen until the music
  stops; a tap still works.
- A talk that interrupted another app has no follow-up window: a second question needs another
  tap.
- Whether the interrupted app goes on is that app's choice; Jarvis can only tell it it may.
- After a talk that interrupted another app, the recorder waits for that app's audio to end
  before it records again, as it does after a call.
