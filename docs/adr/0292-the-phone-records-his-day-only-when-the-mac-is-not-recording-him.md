# ADR 0292 — The phone records his day only when the Mac is not recording him

**Status:** Accepted
**Date:** 2026-10-18
**Amends:** ADR 0238 (when the phone keeps audio), ADR 0256 (the double recording it accepted)

## Context

- ADR 0238 has the phone record his whole day. On 2026-10-14 he turned it off: the battery, and a
  Discord call on the phone could barely hear him. ADR 0256 then had the Mac terminal record the
  room from the reSpeaker, and accepted that with both on, one hour is kept twice.
- At home he sits at the Mac, so there the phone's recording adds nothing and costs the battery.
  Away from home only the phone is with him.
- iOS does not let an app start recording from the background (the reason the control opens the
  app first, see `LifelogIntents.swift`). A phone that stops its engine at home cannot start it
  again on its own when he leaves.
- The Mac may be asleep, closed or disconnected while he is at home. Then nothing records, and
  the only evidence of that on the brain is the archive (pieces are filed by the terminal's name
  and end time) and the terminal's link.
- A day's battery drain by the recorder has not been measured; the phone reports no battery level.
- Recordings are the one stream he does not want lost to a wrong guess: a missed hour cannot be
  made up, a doubled hour costs disk (~22 MB) and nothing else.

## Decision

The phone keeps its audio unless, at that moment, the brain says a connected terminal is
recording and the phone's own fresh position is inside his saved home; every doubt keeps the audio.

- The brain gives facts, the phone decides: whether a terminal is connected and has a freshly
  archived piece, and where home is. The brain does not refuse or drop any piece.
- Doubt means: no answer, offline, no home saved, a position older than 15 minutes or too
  imprecise to say inside, or the terminal's newest piece older than 15 minutes.
- The recorder's engine and session keep running as before; only the writer stops keeping audio.
  A change of state closes the current piece, so a piece never mixes kept and dropped time.
- The phone also reports its battery level, so the recorder's cost over a day can be read from
  the log.

## Alternatives rejected

- **Stop the engine at home and start it again on leaving** — iOS refuses to start recording
  from the background; after the first stop the phone would record nothing for the rest of the
  day until he opened the app by hand.
- **Dedupe on the brain (keep both uploads, drop the phone's copy where a terminal piece
  overlaps)** — the phone still runs the microphone and uploads ~22 MB an hour for nothing;
  the clash with call apps and the battery cost, the reasons it was turned off, stay.
- **The brain pushes "stop" and "start" to the phone** — a recording app has no push channel
  while it records, and a push that arrives late or never leaves the phone in the wrong state.
  A phone that asks at a fixed interval fails safe: silence means keep recording.
- **Decide by Wi-Fi name or by the phone being on the home network** — the brain is reached
  over the tailnet from anywhere, so network presence does not mean he is at home, and
  nothing says the Mac is recording.

## Consequences

- The microphone, and the battery it costs, stay on at home; the saving is storage and upload,
  not power. A switch off, or a measured drain too high, is a later decision on the battery data.
- Up to 15 minutes of the same time can be kept twice around each transition: the Mac's newest
  piece counts as fresh for that long after the Mac stops, and the phone asks only every 2 minutes.
- With home unsaved, location off or Always not granted, the phone records as before: this
  decision only ever removes audio when all three facts agree.
- A position that is fresh and inside home does not prove he is within earshot of the Mac, only
  that the Mac is recording where he lives.
