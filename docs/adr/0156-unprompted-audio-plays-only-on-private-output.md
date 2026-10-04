# ADR 0156 — Unprompted audio plays only on private output

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04: he is often in public with headphones on. Nothing Jarvis
  says or plays without being asked may come out of the Mac's built-in speakers
  or an unknown device, whatever the quiet level (ADR 0153) or the judge (ADR
  0155) decided.
- Two things sound unprompted: the one spoken line for an interview or offer
  mail, and the companion's cue when a notice card appears (agent notices and
  mail cards alike). The cue is played by the client, the line by the daemon,
  and the client polls `/inherent/notices` only every 5 s, so a headphone
  disconnect can fall between a decision and its sound.
- macOS has no "headphones are in" flag. `system_profiler SPAudioDataType -json`
  names the default output with its transport and output source, and costs
  about 0.3 s per call. Probed 2026-10-04: AirPods Pro report transport
  `coreaudio_device_type_bluetooth`, the built-in speakers `coreaudio_device_type_builtin`,
  a Multi-Output Device `coreaudio_device_type_unknown`.

## Decision

Unprompted audio plays only while the default output is private, judged by the
daemon and checked again at the last moment; otherwise the card shows silently.

Its limits:

- Private is a closed list: a Bluetooth device, the built-in headphone jack
  (output source names headphones), or a device whose name says headphones,
  AirPods, buds, earphones or earbuds. Built-in speakers, displays, USB
  speakers, virtual, aggregate and multi-output devices, no default device, a
  failed or slow probe and any other OS are not private.
- The job-mail judge's `card_sound` and `speak` become `card` when the output is
  not private at decision time, and the line is not said unless a fresh look
  right before it still says private. The attention log keeps the judge's own
  level, the device name and its private flag.
- `GET /inherent/notices` serves `card_sound` and `speak` as `card` while the
  output is not private, and says so in a top-level `audio_private`. The
  companion plays no cue while that field is `false` (or the daemon cannot be
  reached) and keeps its old gate when the route or the field is absent.
- The spoken answer to something Allen said is not touched. The companion's
  notice cue is gated whole, so the small sounds of dismissing or answering a
  card are silent on speakers too.

## Alternatives rejected

- **Client-side device check** — the renderer cannot see the output route and
  the daemon speaks the line; a second probe in each place would disagree. The
  daemon already answers every poll, so one flag travels with it.
- **Treating only the built-in speakers as not private** — an unknown output
  (HDMI monitor, USB speaker, a virtual device routing to speakers) would sound
  in public; Allen's rule is that an unknown device is silent, and a closed
  private list costs only a missed cue.
- **Probing on every poll and every sound with no cache** — every client poll
  (5 s) would pay about 0.3 s of `system_profiler`; a two-second cache keeps
  the cost to one probe per two seconds, and only the line's last-moment look
  bypasses it.

## Consequences

- A Bluetooth speaker or a device named like none of the headphone words is
  judged by its transport and name alone: a Bluetooth speaker counts as private.
- A real headphone set on an unusual transport (a wired USB headset named
  "Headset") is treated as speakers until its name matches; the cost is a
  missing cue, never a leaked one.
- Between the probe and the sound there is no hold: a disconnect inside that
  window can still leak, and the client's cue gate is only as fresh as the
  last 5 s poll.
