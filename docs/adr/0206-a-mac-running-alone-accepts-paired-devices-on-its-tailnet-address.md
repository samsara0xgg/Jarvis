# ADR 0206 — A Mac running alone accepts paired devices on its tailnet address

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- ADR 0170 lets only a brain listen beyond loopback. A Mac running alone (`role: all`) refuses
  `runtime.listen_addresses` at boot. So a phone can pair (ADR 0196), report (ADR 0197) and read
  the day (ADR 0199) only on a brain.
- On 2026-10-10 the only brain is the Pi, and it holds none of Allen's state. His reminders,
  conversations and TimeSink history are on the Mac, which still runs alone. The state cutover
  waits on an SSD for the Pi (2026-10-07, "以后再说").
- The phone app is now being built, and it needs a host it can debug against. Shown the choice on
  2026-10-10, Allen picked the Mac ("先连 Mac").
- The phone talks plain HTTP routes under a device token. It needs no terminal link: it declares
  no tools and runs no observers on the host.
- Moving the brain later is a one-time move of the runtime root (ADR 0170). The phone's events,
  stored in the event log, move with that root.

## Decision

Let a Mac running alone listen on its private tailnet addresses, so that paired devices reach its
HTTP routes under their own tokens, the same way they reach a brain.

Its limits:

- **The same guards as a brain.** The addresses must be private, never a wildcard or public. The
  Host check admits only the configured names. A non-loopback peer needs a paired device's
  token. The claim route is the only exception, as in ADR 0196.
- **No terminal link.** `/terminal/ws` stays a brain's. A Mac running alone has no terminal hub,
  so another Mac cannot become its terminal.
- **Nothing else about `all` changes.** Its own tools, voice and observers stay local, and
  "here" still reads the Mac's own location (ADR 0198).
- **Listening is opt-in.** Without the setting the Mac listens on loopback only, as before.

## Alternatives rejected

- **Pair the phone with the Pi brain now.** Its log holds no reminders, conversations or
  TimeSink rows, so the day line would show only what the phone itself sent. Debugging the app
  against it would test an empty timeline.
- **Move the state to the Pi now.** The Pi boots from an SD card, and the SSD that should hold the
  brain's SQLite is not bought. The first live voice test on the Pi (2026-10-08) was worse than on
  the Mac, so his daily voice would get worse too.
- **Make the Mac a `brain` while it keeps its microphone.** The brain role forces every
  device-bound switch off. Voice, the companion and the observers would stop on the machine he
  talks to.

## Consequences

- A closed lid puts the Mac to sleep, and then the phone cannot reach it. The phone keeps its
  unsent events and resends them at a later wake (ADR 0197). The 星盘 shows the last day it
  fetched.
- When the brain moves to the Pi, the phone must pair again with the Pi, because a device token is
  issued by one host. The events it already sent move with the runtime root.
- The Mac now accepts requests from the tailnet. Its exposure is a brain's: anyone holding a
  device token reaches what a device token reaches on a brain, including the Mac's own
  conversation and reminders.
