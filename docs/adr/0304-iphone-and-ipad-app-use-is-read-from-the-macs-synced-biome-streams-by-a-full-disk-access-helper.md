# ADR 0304 — iPhone and iPad app use is read from the Mac's synced Biome streams by a Full-Disk-Access helper

**Status:** Proposed
**Date:** 2026-10-19
**Supersedes:** none

## Context

- Allen's phone and iPad are where much of his day happens, and nothing in Jarvis sees which app
  he was in. With Screen Time "Share Across Devices" on, macOS syncs those devices' app focus
  events into the Mac's Biome store, in
  `~/Library/Biome/streams/restricted/App.InFocus/remote/<device id>/`.
- The files are SEGB segments (v1 and v2 both exist), each record a protobuf `AppInFocus` event.
  Open-source readers document the format: nichtlegacy/screentime (`sources/biome.py`, vendored
  CCL `ccl_segb`) and ActivityWatch/aw-import-screentime (`app_in_focus_extended.proto`).
  Fields used: 3 = 1 gained focus / 0 lost it, 4 = CFAbsoluteTime double (seconds since
  2001-01-01 UTC), 6 = bundle id. Records pruned by macOS stay as zeroed state-3 entries.
  Device names are in `~/Library/Biome/sync/sync.db` (`DevicePeer`).
- The folder is TCC-protected: reading needs Full Disk Access, and macOS grants it per app, so a
  helper run through Python would put the grant on the Python interpreter.
- The Mac keeps about four weeks of it, and a phone's events reach the Mac with lag, sometimes
  hours.

## Decision

Read the Mac's remote `App.InFocus` streams with `native/screen_time`, a Swift app bundle built
on demand and ad-hoc signed as `com.allen.jarvis.screen-time` (the ADR 0194 pattern), and have
it write one JSON object of devices and focus events.

- `jarvis.surface.mac_screen_time` launches it with `open -W -n` and returns `{"devices",
  "events"}`; an event is `{device, bundle_id, focus: "in"|"out", at_ms}` in unix milliseconds.
  The records hold transitions, not durations, so pairing them into intervals is the caller's job.
- It reports `no_access` (Full Disk Access not granted) or `no_remote_streams` (nothing synced) as
  an error instead of events. It reads remote devices only, never this Mac's own stream.
- `python -m jarvis.surface.mac_screen_time probe` prints per device the event count, first and
  last time and the top apps per day, to check completeness against the phone's own Screen Time
  numbers.
- Shipping the events to the brain and putting them on the day line are separate decisions.

## Alternatives rejected

- **Shortcuts automations on the phone** — one automation per app, so nothing he has not listed
  is seen; they cannot cover apps installed later; iOS 26.6 closes some of them spuriously. Kept
  as a possible realtime supplement, since synced data lags.
- **The Screen Time API (FamilyControls, DeviceActivity)** — apps appear only as opaque tokens,
  the report runs in a sandboxed extension that cannot hand data to the app, and there are no
  timestamps.
- **Full Disk Access for the Python interpreter** — every script run with it would read the whole
  disk.

## Consequences

- He must turn on Share Across Devices on each device and grant Full Disk Access to
  `JarvisScreenTime.app` once.
- Signed with the Mac's Apple Development identity, the helper keeps one designated requirement, so the grant survives rebuilds; with no such identity it is signed ad hoc and a rebuild may need the grant again.
- Data lags by the sync delay, and macOS drops it after about four weeks, so it has to be read
  regularly or the older days are lost.
- The stored CRC32 of a SEGB record is not checked; a malformed protobuf is skipped instead.
- Whether the remote streams exist on this macOS without Full Disk Access to look is unverified.
