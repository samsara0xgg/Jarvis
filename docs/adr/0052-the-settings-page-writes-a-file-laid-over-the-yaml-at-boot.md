# ADR 0052 — The Settings page writes a file laid over the YAML at boot

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** none

## Context

- Allen asked on 2026-09-25 for the companion's Settings page to work,
  microphone choice included. Its Jarvis items are the reply language, the
  wake threshold, the TTS voice and volume, speaker, microphone, GPT-Live,
  Mac echo cancellation, TimeSink, keeping voice recordings, and read-only
  repos and models.
- Every one of those values is read from `config/jarvis.yaml` once, when
  the daemon builds its wake engine, TTS client, players, ingress, observers
  and memory settings; none has a path to change while the daemon runs.
- `config/jarvis.yaml` is tracked in git, carries long comments, and is
  becoming the shipped defaults of a public app (2026-09-25). A YAML
  round-trip through PyYAML drops every comment.
- The daemon restarts in seconds under launchd's KeepAlive, and the page
  already has a Restart button (ADR 0051).
- The input stream opens the system default input and reopens when that
  default changes; the reSpeaker and the MacBook microphone are both
  present. MiniMax's `vol` below 1 did not lower the audio it returned
  (measured 2026-09-25), so a quieter voice has to be made on our side.

## Decision

Keep Jarvis's own settings in a daemon-owned `<runtime root>/settings.json`
that the Settings page reads and writes through `/inherent/settings`, laid
over the YAML config once at boot so a saved change applies on the next
restart; the file holds only keys the page offers, each checked on save and
again at boot, and a key the file does not hold keeps its YAML value. A
chosen microphone or speaker that is missing at boot fails closed instead of
falling back to the system default.

## Alternatives rejected

- **Write the choices into `config/jarvis.yaml`** — PyYAML would drop the
  file's comments on the first save, and a personal choice would show up as
  a diff in the tracked defaults.
- **Apply each change live** — each of the ten values is baked into an
  object built at boot; ten reload paths through the audio and observer
  stacks cost more than one restart that already exists.
- **Environment variables in `~/.jarvis/env`** — that file holds secrets and
  is not something the desktop should rewrite.
- **Fall back to the default microphone when the chosen one is gone** — it
  would listen on a device Allen did not choose without telling him; the
  speaker setting already refuses that.

## Consequences

Every change waits for a restart, and the page says so. There are now two
places a value can come from, and the file wins; reading the YAML alone no
longer tells what a running daemon uses. A microphone unplugged at boot
leaves Jarvis deaf until it is plugged back or the choice is reset. The
reply language is one line at the end of the conversation prompt; GPT-Live
keeps its own instructions and is not changed by it. The voice list is
MiniMax's Mandarin system voices as of 2026-09-25, kept in code.
