# ADR 0170 — One brain holds the owner's state and every other device is its terminal

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- One process, `python -m jarvis serve`, does everything on the owner's
  MacBook: microphone, echo cancellation, ASR, playback, memory, the event log,
  every model request and every tool. It listens only on 127.0.0.1:8006
  (ADR 0014 D5). When the lid closes, Jarvis stops: no reminder fires, and the
  phone has nothing to talk to.
- On 2026-10-06 (thread "Jarvis 生态部署") the owner approved "one brain plus
  terminals, no data sync": an always-on host is the brain, and the MacBook and
  iPhone are terminals over Tailscale. The host is a Raspberry Pi 5 (4 GB,
  Debian 13 aarch64, boots from SD). The owner decided not to buy a Mac mini for now,
  so the brain host has to be swappable later without touching terminals.
- State is the identity (C2). The event log is append-only and written by one
  process, and every projection folds that one ordered log. Two copies of
  `memory.db` and the log on two machines would need merge rules for memory
  records, pending confirmations and cards, and none exist.
- Some work cannot leave the device it serves:
  - echo cancellation runs where the microphone and speaker are;
  - playback runs where the speaker is;
  - `open`, `screencapture`, `pbpaste` and `mdfind` act on that device;
  - observers read local files (TimeSink, `~/.claude`, repositories);
  - Claude Code and Codex sessions are the owner's work on that Mac.
- Spoken turns open about 2.4 s after the owner stops (after ADRs 0164-0166).
  SenseVoice runs on the M2 Max's cores, which are far faster than the Pi's four
  Cortex-A76. A network hop between terminal and brain is estimated at about
  10 ms at home and 50-150 ms away; this is not measured.
- Two parts assume playback runs in the same process: the timeline that
  captions read (ADR 0165), and the heard prefix that barge-in keeps (ADR 0083).
- Linux has no login Keychain, where API keys live today.
- `docs/spec.html#positioning` lists the Raspberry Pi and cross-device
  federation as non-goals of this version.
- A public build must still install on one Mac and work with no second device.
- The v2 protocol (ADR 0014) already serves several clients of one daemon. It
  gives each client a `client_instance_id`, makes submits idempotent and resumes
  from snapshots.

## Decision

Run Jarvis in one of three roles, `brain`, `terminal` or `all`, so that
exactly one brain holds the owner's state and every other device is a terminal
of that brain. `all` is the default and is today's single machine.

Its limits:

- **The brain owns state and egress.** These live on the brain and nowhere else:
  - the event log, `memory.db`, settings and API keys;
  - every model, speech, web and MCP request;
  - Jev, proactivity and scheduling.

  A terminal holds no model key and no copy of the owner's state. Its runtime
  root keeps only its own config, logs and the audio it heard.
- **The terminal owns the device.** It owns the microphone, echo cancellation,
  wake word, endpointing, ASR where the device can run it, playback and the UI.
  It also owns the device-bound tools and the observers of local files.
  - On connecting, a terminal declares the tools it can run.
  - The brain sends a device-bound call to a connected terminal that declared
    it. With none connected, the tool result says the device is not there.
  - Observers push events into the brain's log, tagged with their device.
- **Voice splits at text.** A terminal that runs ASR sends text, partials
  included. One that cannot (the phone) uploads the utterance for the brain to
  transcribe.
  - The brain synthesizes speech and streams the audio to the terminal that
    spoke.
  - That terminal reports playback progress, so the timeline and the heard
    prefix stay on the brain.
  - Audio stays on the device that heard it, except in the upload case.
- **One brain, no sync, no standby.**
  - A terminal pairs with exactly one brain.
  - When the brain is unreachable, a terminal says so. It never falls back to
    a brain of its own.
  - Moving the brain to a new host is a one-time move of its runtime root.
- **A private network only.** Terminals speak the v2 protocol to the brain.
  - The brain listens on loopback and its tailnet address, never on a public
    interface.
  - Each terminal gets its own pairing token, revocable alone.
  - A remote terminal never reads the brain's token file.
- **One owner.** In C1's sense, a brain and the terminals paired to it are one
  install. Its owner is the person who pairs them. Nothing else in ADR 0090
  changes.

## Alternatives rejected

- **Every device runs a full Jarvis and they sync.** The log has one writer and
  no merge rule. A confirmation card shown on both devices could be answered
  twice and execute twice. Memory written on the phone while the Mac is asleep
  would need conflict resolution that C2 has no answer for.
- **The Mac stays the brain and the Pi relays or stands by.** The case that
  motivates this decision is the closed lid, and then the state is unreachable.
  A standby brain that takes over is the sync problem again.
- **The Mac ships raw audio and the brain runs ASR.** Echo cancellation has to
  stay next to the speaker either way. SenseVoice on four Cortex-A76 cores adds
  time between endpoint and text on every turn, compared with the M2 Max. Every
  utterance's audio would also leave the Mac.
- **A cloud server as the brain.** The owner's state would live off the owner's
  premises, and Jarvis would need a public listener. The spec's non-goal "not a
  cloud service" rules this out.

## Consequences

- Device-bound tools and observers can now fail because the device is offline.
  The model hears that as a plain fact and tells the owner.
- A split deployment adds a network hop to every spoken turn. Whether the
  owner's daily voice moves to a split deployment is decided by measuring it
  against `all`.
- Captions and the heard prefix depend on playback reports arriving on time.
  Before the split they come from inside the same process.
- Proactive speech plays only on private output (ADR 0156). The terminal now
  reports whether its output is private, and the brain picks which device
  speaks.
- A brain on Linux needs four things:
  - keys from a 0600 file instead of the Keychain;
  - a service manager instead of launchd;
  - a lock file that resolves on linux-aarch64;
  - a Host allowlist that admits its tailnet name.
- Gmail and Microsoft sign-in need a path that works on a brain with no
  browser.
- The brain is a single point of failure. While it is down, every device
  answers "the brain is not reachable".
- A brain that boots from an SD card wears it with constant SQLite writes. The
  host needs an SSD before it holds the owner's state.
