# ADR 0238 — His recorded day is kept on the brain as a raw archive the phone sends piece by piece

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- On 2026-10-11 in thread 「24 小时录音放哪」 Allen asked for a recording of his waking day
  (talking to himself, rehearsing interview answers, people around him), to be handed to future
  models to analyse and to rebuild what happened. He said 「动手 现在就做」 at 22:19Z.
- The only device that is always with him is the iPhone. The Mac's disk is nearly full. The brain
  is the Pi (ADR 0170), with one memory and no sync between devices. An NVMe SSD for the Pi is
  ordered; until it is fitted the runtime root is on a 59 GB SD card with about 46 GB free.
- Recording stays on only while he wants it: he chose a manual switch on the phone (Control
  Center, the Dynamic Island mark, or telling her), with no automatic start or stop by Sleep
  Focus. The capture side, its encoder and its switch belong to the phone app.
- Speech at Opus 48 kbps stereo is about 22 MB an hour, so a waking day is about 350 MB and a
  year about 125 GB. A piece of ten minutes is about 4 MB.
- iOS gives an app a few seconds when it wakes in the background and may end it at any time, so
  an upload can be cut off and repeated. The phone must not delete its copy before the brain has
  every byte.
- What a future model can draw from the audio is unknown, so nothing is dropped or cleaned when
  it is kept: no noise suppression, no trimming of silence, no transcoding.
- ADR 0067 sweeps recordings by age, and its export is one zip built in memory and sent over HTTP.

## Decision

The brain keeps every piece a paired phone records of his day, byte for byte and for good, in
one archive filed by device and UTC start.

- A piece arrives on a route that only a paired device's own token opens. Each piece has an id the
  phone chose. A piece the archive already holds with the same bytes is answered as kept. The
  same id with other bytes or another start is refused.
- A piece is written whole or not at all, and the answer carries its SHA-256, so the phone deletes
  its copy only when the digest matches its own.
- The archive takes only an audio container it recognises from the first bytes. It does not judge
  the codec inside. It refuses a piece that would leave the disk below a floor, so the brain's own
  databases never run out of room; the phone keeps the piece and sends it later.
- The archive is outside retention, outside "clear recordings" and outside the export. Only
  erasing all data removes it, and only while it sits under the runtime root.
- The archive sits in the runtime root by default, so it moves with the root to the SSD. A setting
  can put it elsewhere.

## Alternatives rejected

- **Phone's Voice Memos synced through iCloud** — iCloud puts the audio with Apple, and Voice Memos
  stops at every call or microphone hand-off without resuming. It is kept only as a stopgap
  before the app records, with its files imported afterwards.
- **Record on the Mac or the Pi's own microphone** — each hears one room. He is away from both for
  most of the day, so most of his day would never be recorded.
- **An AI recording pendant (Plaud, Limitless, Bee)** — these upload to their makers' clouds by
  default, which breaks keeping his data on his own machines.
- **Keep only speech (voice activity detection on the phone)** — about a third of the bytes, but
  silence, music and room sound are thrown away for good, and they are part of what a later model
  may read.
- **Send through `POST /inherent/attachments`** — capped at 4 MiB, deleted after 30 days, cleared
  with the recordings, and its files are made to be read by a turn, not kept as an archive.
- **Put the pieces in the export zip** — a year is about 125 GB. The zip is built on the brain's
  disk before it is sent and would need that much free space again.
- **Index the pieces in a database** — the file name already holds the device, the start, the
  length and the id. An index would be a second copy of those facts that can drift from the files.

## Consequences

- Until the SSD is fitted, the archive fills the SD card at about 350 MB a recorded day. Refusals
  begin once less than the floor (5 GB by default) would remain. The phone holds what is refused.
- Nothing on the brain reads the archive yet. Transcription and the day line read it in a later
  change, from the archive as it is, and can be run again with better models.
- The archive holds other people's voices. California requires every party's consent to record a
  confidential conversation. The switch is his, and he turns it off for such talks.
- The archive is backed up by nothing yet; a lost disk loses it.
