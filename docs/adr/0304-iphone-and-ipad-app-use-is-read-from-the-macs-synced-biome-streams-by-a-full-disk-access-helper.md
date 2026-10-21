# ADR 0304 — iPhone and iPad app use is read from the Mac's synced Biome streams by a Full-Disk-Access helper

**Status:** Accepted
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

### Shipping and the day line

The Mac terminal's shipper (ADR 0294) gets a step for a new shipped source, `device-apps`, that
runs at most every 15 minutes and sends two kinds of entry:

- `device_app`, one per **closed** stretch of one app on one device: `{device_id, device
  ("iPhone"/"iPad", from the model), bundle_id, start_ms, end_ms, unpaired}`, key
  `device_app:<device id>:<bundle id>:<start_ms>:<12 hex of the fields>`, so a send again is the
  same row. A stretch whose "out" has not synced yet is not sent; the device's cursor stays at
  its start (else just past the device's newest event) and the next read starts there, 10 minutes
  early. An open stretch older than a day is given up on and sent cut at 30 minutes, `unpaired`.
  Macs are skipped (TimeSink covers them), and so are system overlays (Control Center, the app
  library, SpringBoard alerts) and stretches under 5 seconds.
- `device_horizon`, when the time of a device's newest event moves: `{device_id, device,
  synced_ms}`. Records reach the Mac hours late, so this is how far each device is known; what
  lies after it is unknown, not "not used". It is a lower bound: a device nobody touched looks as
  if it were behind, because the records say when he used it, not when it last synced.
- `app_name`, once a bundle id is resolved: `{bundle_id, app, category}`, key
  `app_name:<bundle id>:<12 hex>`, sent again only if the resolved value changes. Names are
  separate rows so that a failing lookup never holds back the stretches. They come from a table
  of Apple's own apps (named in the settings language), else Apple's public lookup
  (`itunes.apple.com/lookup`, the store of the settings language first: China for `zh`, US for
  `en`, the other after) on the Mac, at most 20 lookups a pass (3 seconds apart, this pass's
  bundles first), cached beside the shipper's state. A miss is asked again after 7 days; a bundle
  the store could not be asked about (network error, 403, 429, 5xx, or no lookups left) is asked
  again next pass.

The day line (ADR 0199) folds these into kind `screen`, in his lane: one item per block of
foreground use on one device, stretches of any app joined when the gap is at most 5 minutes,
blocks under 5 minutes of foreground time dropped. Fields `device`, `active_s` (foreground
seconds, not end minus start) and `apps` (top five by time: `{name, bundle, minutes, category}`, name and category joined by bundle id from the newest `app_name` row, else the bundle id and null);
id `screen-<device>-<start_ms>`. The day's answer also carries `screen_synced_ms`, the devices
whose newest record is before the end of the window (or now), so a stretch not yet synced reads
as unknown. With a shipped store but no device record at all, `screen` is in `missing`. `screen`
is a kind the phone may have open (ADR 0214, 0298).

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
- An app with no `app_name` row yet (store down, not listed) shows as its bundle id.
- Data lags by the sync delay, and macOS drops it after about four weeks, so it has to be read
  regularly or the older days are lost.
- The stored CRC32 of a SEGB record is not checked; a malformed protobuf is skipped instead.
- Whether the remote streams exist on this macOS without Full Disk Access to look is unverified.
