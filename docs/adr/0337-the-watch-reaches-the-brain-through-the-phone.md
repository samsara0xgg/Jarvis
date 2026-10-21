# ADR 0337 — The watch reaches the brain through the phone

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- 2026-10-21 yilun asked for a Jarvis app on his Apple Watch: today's ring with her in the
  middle, what waits on him answered from the wrist, the bus he is on, complications, and
  talking to her.
- The brain answers only on the tailnet (spec: `brain` in the pairing answer is
  `http://host:port`). watchOS has no VPN, so the watch cannot join the tailnet, and its own
  traffic is not guaranteed to ride the phone's VPN when it goes through the phone.
- The phone already holds the pairing token, reaches the brain, and turns the brain's day into
  rows, colours and words. WatchConnectivity carries messages and a latest-state context between
  the phone app and its watch app; iOS wakes the phone app in the background for a watch message.
- `POST /inherent/ask` takes words and returns her spoken and written answer within 25 s; it
  carries no audio.
- A watch with cellular, away from the phone, could reach the brain only over a public HTTPS
  route guarded by a device token. That opens the brain to the internet, which is a security
  decision yilun has not made (2026-10-21: pending, to be asked).

## Decision

The watch talks only to the phone, over WatchConnectivity, and the phone relays to the brain over
its existing pairing; the watch holds no token and reports nothing of its own.

- The phone pushes the day as the watch draws it (ring blocks, what is now, what waits, what
  comes next, the ride) as the application context, again whenever it changes, so the watch
  opens with it already there.
- The watch's answers to what waits go to the phone, which answers through its own paths.
- Talking: the watch streams its microphone to the phone, the phone recognises the words and
  ends the turn on a pause as it does for its own voice, then asks the brain with
  `/inherent/ask`. Her answer comes back to the watch as text; the watch speaks nothing until the
  brain can return her voice for an ask.
- A phone-free route (public HTTPS with a device token) is pending yilun's security decision;
  until then the watch without its phone shows the last day it was given and says the phone is
  out of reach.

## Alternatives rejected

- **The watch calls the brain directly** — no route exists: the watch cannot join the tailnet, and
  WatchConnectivity is the only channel the watch app is guaranteed to have to the phone.
- **A public route now** — it exposes the brain on the internet before yilun has decided whether
  and how to guard it.
- **Recognise speech on the watch** — the phone's recogniser (two on-device transcribers, its
  endpointing) is the one his voice turns are tuned on; a second recogniser on the watch would
  split the tuning and the failures.
- **Speak her answer with the system voice** — it is not her voice; she would sound like
  someone else on the wrist.

## Consequences

- With the phone out of reach or its app killed and not woken, the watch can answer nothing and
  talk to nobody; it shows the last day it was sent.
- Every watch talk is a turn of the phone's device name in the log (ADR 0212), so "here" for a
  watch turn is the phone's report.
- Recognition depends on the phone app running in the background when woken by the watch; if iOS
  does not keep it alive long enough, a watch talk fails and says so.
