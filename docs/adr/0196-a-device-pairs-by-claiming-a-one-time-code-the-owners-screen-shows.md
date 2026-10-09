# ADR 0196 — A device pairs by claiming a one-time code the owner's screen shows

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- ADR 0170 pairs a terminal with `python -m jarvis pair <name>` run on the brain. It prints the
  token once, and the owner carries it to the terminal as a file. A Mac terminal has a shell.
  A phone has none, and Allen does not type commands (2026-10-07).
- Allen approved a phone app as a terminal of the brain on 2026-10-09 and chose scanning a QR
  code over an account login ("简化点现在二维码也可以").
- The brain is reached only over the tailnet. A peer that is not on loopback needs a paired
  device's token on every route except the liveness probe. A phone that has never paired has
  no token, so its first request must get through without one.
- The brain is meant to be a headless host with no screen (ADR 0170). Allen's screen is a
  terminal Mac, whose companion reaches the brain only under that Mac's device token (ADR 0183).
- A QR code holds a few hundred bytes, so the secret in it can be as long as a token. A code a
  person types has to be short, and a short code needs guessing limits.

## Decision

Pair a device with a one-time code that any caller the brain already admits can mint, that the
owner's screen shows as a QR code, and that the new device exchanges for its own token on one
route open without a token.

Its limits:

- **A code is as strong as a token.**
  - It is as long and random as a device token.
  - It names the device it will pair.
  - It is valid for ten minutes and for one claim.
  - Minting another voids it.
  - Codes live only in the brain's memory, so a restart voids them.
- **Minting is open to every admitted caller.** That means the local key, or any paired device's
  token. The brain refuses to mint while it listens on no private address, since nothing could
  reach it to claim.
- **The claim route is the only exception.** Besides the liveness probe, it is the only route a
  peer without a token reaches. A wrong, used or expired code all get the same refusal.
- **The QR holds only the code and the addresses the brain answers on.**
- **A claim yields the same token `pair` mints.** It is one per device, stored as a hash, and
  revoked by unpairing.
- **Device management.** The same callers can list paired devices and unpair one. The brain's
  `pair`, `unpair` and `devices` commands stay.

## Alternatives rejected

- **An account login with a password.** The brain would have to store a password short enough to
  remember, and so to guess. Who may reach the brain at all is already decided by the tailnet;
  a second account adds a secret to manage without narrowing who gets in.
- **A short typed code (six digits).** That is 10^6 values. At 1,000 requests a second, any device
  on the tailnet exhausts them in under 17 minutes, so the code would need rate limits and
  lockouts. A 256-bit code in a QR needs neither.
- **Minting only on the brain's loopback, or with its local key.** The brain has no screen to show
  the QR on. The screen Allen looks at belongs to a terminal Mac, whose companion cannot present
  the brain's local key (ADR 0183).
- **Minting on the brain's command line and printing the QR there.** This needs a shell on the
  brain, and Allen does not type commands.

## Consequences

- Anyone who can see the owner's screen while the QR is up, and is on the tailnet, can pair a
  device in those ten minutes.
- Any paired device can pair more devices and unpair others, including itself. A device token
  already opens every owner-state route, so this adds no reach, but it does make a stolen token
  harder to lock out: the thief can pair a second device before the first is revoked.
- A Mac running alone (`role: all`) listens on loopback only, so no phone can pair with it. A
  phone needs a brain that listens.
