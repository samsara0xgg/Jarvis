# ADR 0375 — The day's recording keeps the app awake across a hand-over and tells him when it stops

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- He turns the phone's day recording (ADR 0238) on to keep moments he wants, such as a driving
  lesson. On 2026-10-20 (19:06) and 2026-10-21 (09:58) it stopped in his pocket and stayed off
  until he next opened the app, more than an hour each time, and nothing told him.
- The phone's `lifelog.log` and the code agree on the cause. He switched the recording on with the
  app open, so the in-app wake word's engine owned the microphone and fed the recording. Locking the
  phone disarms the wake word, whose engine stops and hands the microphone to the recorder's own
  engine. In that moment no audio runs, iOS froze the app, and the recorder's queued start ran only
  when location woke the app seven minutes later, in the background: engine error `'what'`, then
  `setActive` refused with `'!int'` every 10 s for eight minutes, then an interruption that never
  ended. The process stayed alive throughout.
- iOS freezes a background app with no audio running within seconds. A frozen app cannot be
  relied on to start the microphone again in the background. Only his opening the app, an
  interruption's end, or an `AudioRecordingIntent` run with a Live Activity can start it again.
- He wants the recording out of sight: no Live Activity while it runs (LifelogIntents.swift).
  On 2026-10-21 he chose to be told by a notification when it stops.
- ADR 0348 still holds: for a call, or another app's audio, the recorder gives way and does not
  take the sound back.

## Decision

While the switch is on, the phone keeps itself from being frozen until the recorder's engine runs
whenever the microphone changes hands or the engine is rebuilt. If audio stops anyway, a
time-sensitive local notification tells him, with the reason when the phone knows it, and a tap
opens the app, where the recorder starts again. The notification is queued about 90 s ahead and
pushed back by every stretch of audio, so it also fires when the app is frozen, stuck or killed.
A pause for a call or another app's audio (ADR 0348), or the switch going off, takes it away.

## Alternatives rejected

- **Keep retrying the start in the background** — the 10-21 log shows 47 refusals in a row over
  eight minutes and no success until the app was opened. The phone now stops after three tries
  and tells him.
- **A Live Activity for as long as it records** — this is the only route iOS gives for starting
  the microphone from the background, but it keeps the Dynamic Island occupied all day. He chose
  the notification instead on 2026-10-21.
- **Tell him only from inside the app when it notices a stop** — a frozen app notices nothing. On
  10-21 the app ran no code at all from 09:58 to 10:05, so the warning has to be queued with iOS
  in advance.

## Consequences

- If he kills the app with the switch on, the notification fires about 90 s later. That is true
  (nothing records) but can surprise him.
- If the recorder gives way to another app's audio while the phone is in the background, it still
  cannot start again by itself after that app's audio ends, and no notification fires for it.
- The app now needs the time-sensitive notification entitlement.
