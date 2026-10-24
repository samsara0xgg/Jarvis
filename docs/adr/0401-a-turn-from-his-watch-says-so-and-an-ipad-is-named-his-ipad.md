# ADR 0401 — A turn from his watch says so, and an iPad is named his iPad

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- Each turn's state line says how he reached her, on which device, and how the answer goes back
  (ADR 0226). The device's kind comes from what the host has seen it do (ADR 0217): a terminal is
  a computer; a device that registered for pushes or has its conversation open is his phone.
- The watch has no route of its own to the brain. The phone hears the wrist's microphone,
  recognises the words and asks the brain with `POST /inherent/ask` under its own pairing, and her
  answer goes back to the wrist as text (ADR 0337). The ask carries only `{"text"}`, so the turn
  is written as words typed on the phone.
- On 2026-10-23 at 19:49 he spoke to her on his watch for the first time. Her state line read
  "This turn: typed on his phone (iphone-2); your answer is shown there, not read aloud.", and she
  answered 「这条是你在手机上打的字」. Told it was the watch, she said she could not see that.
  Both facts in the line were wrong: he spoke, and he was on the watch.
- The same iOS app runs on an iPad (ADR 0338). An iPad registers for pushes and opens its
  conversation like the phone, so it would be named his phone. Its kind is one fact the app
  already holds, the device's idiom; the host has no way to tell it otherwise. The device name
  is whatever was typed when it was paired.
- On 2026-10-24 Allen asked that the watch and the iPad be told apart the way the computer and
  the phone are.

## Decision

The device says what it is in what it already sends, and the turn's state line names it.

- **The watch.** When the phone asks on the wrist's behalf, the body carries `"via": "watch"`.
  The turn's opening row keeps it (`surface.user_intent`'s `via`), and its state line reads
  "This turn: spoken to his watch, relayed by his phone (iphone-2); your answer is shown on the
  watch, not read aloud." followed by the misheard note, since the words came through speech
  recognition. The turn stays the phone's in every other way: its device, its "here" (ADR 0337),
  its cards. Any other value of `via` is refused with 400; an ask without it is unchanged.
- **The iPad.** Its push registration (`POST /inherent/device/push`) carries `"idiom": "pad"`,
  a phone's `"idiom": "phone"`. The host keeps it with the registration; a registration without
  it keeps the stored one, and none stored is a phone. A device whose registration says `pad` is
  named "his iPad" in her devices line and in a turn's state line ("typed on his iPad (ipad)").
- The system prompt is not changed: it already says the answer goes back to the device he used,
  and the state line says which device and how.

## Alternatives rejected

- **Tell her in the system prompt that a typed turn from the phone may be the watch.** It would
  make every phone turn uncertain, and she still could not tell which one was.
- **Pair the watch as a device of its own.** It holds no token and cannot reach the brain
  (ADR 0337); a second name for the same relay would split its turns, cards and "here" from the
  phone that actually carries them.
- **Tell an iPad by its pairing name.** The name is free text typed on the Mac when pairing.
- **Send the idiom at pairing.** Devices paired before this would never send it; the push
  registration is made again at every launch.
- **A new route for what a device is.** One fact that every paired iOS device already sends a
  registration for does not need a route of its own.

## Consequences

- The push route refuses a key it does not know, so a brain must be updated before any app
  build that sends `idiom`; until then that device's registration fails and it gets no pushes.
  `via` is safe in either order: the ask route reads only the keys it knows.
- An iPad that never registers for pushes (notifications declined) is still named his phone
  while its conversation is open.
- A watch turn's words are the phone's recognition of the wrist's microphone, so they may be
  misheard like any spoken turn.
