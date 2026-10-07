# ADR 0183 — A terminal serves its device's UI on loopback and is its only path to the brain

**Status:** Accepted
**Date:** 2026-10-07
**Supersedes:** none

## Context

- ADR 0170 gives the device's UI to the terminal. ADR 0172 runs voice on the
  terminal. The owner approved building the remaining steps on 2026-10-07.
- The companion (Electron) reaches the daemon only through 127.0.0.1:8006.
  - It uses about fifty `/inherent/*` HTTP routes and one push socket,
    `/inherent/ws`.
  - It authenticates with the token in the runtime root.
  - On start, it adopts whatever answers on 8006 with that token. When nothing
    answers, it starts a daemon itself.
- Some of what the UI shows comes from the device itself:
  - partial captions, the listening and speaking faces, and the playback
    position (the `voice` ops of the capture session and the media actor);
  - dictation, which records on the local microphone and runs local ASR;
  - the microphone and output device choices, and the mic and speech mute
    switches.
- Everything else the UI reads or changes is the owner's state, which lives on
  the brain: cards, notices, mail, jobs, memory, settings, confirmations and
  the Dashboard numbers.
- The brain admits a remote peer only with that device's own pairing token
  (ADR 0170). The terminal already holds it. Six routes also require the
  brain's local token, and a device token never opens them: plugin
  credentials, language, Codex reset and balances.
- Measured on 2026-10-07: from the MacBook on campus, the brain answered in
  about 80 ms over a Tailscale relay, against 3 ms on loopback.

## Decision

On a device with a UI, the terminal serves the UI's interface on loopback,
answers device routes itself, and forwards everything else to its brain.

Its limits:

- **One local endpoint.** The terminal listens on 127.0.0.1 only, on the port
  a daemon would use, and checks the same local tokens a daemon checks. The
  UI cannot tell it from a daemon, so the companion adopts it unchanged.
- **Forward by default.**
  - Each HTTP request and the push socket go to the brain under the
    terminal's device token.
  - A route the device token cannot open answers the UI with the brain's
    refusal. It is not retried with any other credential.
- **Device routes stay local.** A small fixed set is answered by the terminal
  from its own process: dictation, restart, and the microphone and speaker
  part of two pages. Settings keeps the brain's page and takes the two device
  choices from this machine; controls throws the mic and speech switches here
  and leaves conversation, quiet and live to the brain. The voice preview is
  not one: the brain holds the speech key, and the companion plays the audio
  it is sent.
  Dictation records and recognises here; its polish is a model call, so the
  terminal asks the brain for it over the link, where the key is.
- **The push socket is merged.** The UI gets the brain's ops and the
  terminal's own `voice` ops on one socket, and the local ops carry no network
  delay.
- **No second brain.** A terminal never answers an owner-state route from
  local data. While the brain is unreachable, those routes fail with "the
  brain is not reachable", and the UI shows that.
- **Exclusive with a daemon.** A terminal serves the UI only when no daemon
  holds the port on that device.

## Alternatives rejected

- **The UI talks to the brain directly.** The companion would need two base
  URLs and two credentials: the brain's for state, and a local one for
  dictation and the device routes. Partial captions and the speaking face
  would cross the link twice, terminal to brain to UI. Over the measured 80 ms
  relay, that is at least two hops on every caption update.
- **A generic reverse proxy** such as `tailscale serve` or Caddy. It cannot
  answer device routes locally or merge the terminal's voice ops into the push
  socket. It also cannot check the companion's local token, so any local
  account could reach the brain through it.

## Consequences

- The terminal now listens, on loopback only, and the spec's "终端不监听"
  changes with this decision.
- Every UI read pays the link's round trip. Lists and Dashboard numbers that
  load in a few milliseconds today take about 80 ms over a relay.
- The six routes that need the brain's local token have no path from a
  terminal's UI. Plugin credentials, language, Codex reset and balance records
  are set on the brain until a later decision gives them one.
- A new route the companion adds must be classed as device or forwarded.
  Unclassed routes are forwarded.
