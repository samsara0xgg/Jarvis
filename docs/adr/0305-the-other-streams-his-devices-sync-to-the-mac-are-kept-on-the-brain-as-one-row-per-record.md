# ADR 0305 — The other streams his devices sync to the Mac are kept on the brain as one row per record

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

- ADR 0304 reads only app focus from the Mac's synced Biome store. The same sync carries more of
  what his iPhone and iPad do: what is playing and where the sound goes (`Media.NowPlaying`, about
  four weeks), when his AirPods connect and their battery (`Device.Wireless.Bluetooth`, about four
  weeks), and what Siri remembers of his messages, calls and media requests
  (`Siri.Remembers.MessageHistory`, `CallHistory`, `InteractionHistory`, `AudioHistory`, about a
  year). They are the same SEGB segments as 0304's, read by the same Full Disk Access helper, and
  they reach the Mac with the same lag. Which field means what was decoded from his own Mac, not
  from a published schema, so one field is unconfirmed: the order of the three battery levels.
- Allen asked for every piece of data his devices give to be stored on the brain, cleanly, for
  later layers (the doing lane, the day's writer) to analyse. He approved storing who he messaged
  or called and when.
- Records share a millisecond or a Siri uuid: of 17,354 distinct now-playing times on his Mac,
  1,715 belong to more than one record, and so do 741 of 20,796 Siri uuid-and-time pairs. The
  brain's newest-revision rule (ADR 0294) reads a key without its content hash as one row.

## Decision

Read the three streams with the same helper as 0304, ship each record as its own row under a new
source `device-records`, and keep interpretation out of ingestion.

- A row is a record with its fields named, nothing summarised, not even what looks like noise;
  kinds are `now_playing`, `bluetooth`, `message`, `call` and `intent` (the other two Siri
  streams). Its key ends in the digest of its fields as 0294 requires and carries, before it, the
  time or uuid and a few digits of that digest, so records that share a moment stay rows of their
  own and a send again is the same row. A device that is not an iPhone or iPad is skipped, as in 0304.
- The first read, and the first read after a device or stream is met, takes all the Mac holds,
  the year of Siri included. Later reads start before the oldest per-stream cursor and are never
  more than three days deep, so a quiet stream does not make every pass long; a record that
  reaches the Mac later than that is not seen again. Cursors and each row's memory move only after
  the brain kept the batch.
- Text is kept whole up to 4 KB a row, then cut and marked. That is the album, post or lyric field
  of now-playing, which holds the text of what he was looking at or hearing.
- The names of apps stay the `app_name` rows of `device-apps`; this step resolves the bundles it
  meets and sends them there, so the day line has one place to look.
- The day line draws four kinds from these rows, in his lane: `media`, `earbuds`, `messages` and
  `phone_call` (not `call`, which is TimeSink's). `intent` rows are stored and not drawn. The
  kinds are folded when the day is asked for, as 0199 does; the thresholds belong to the code.
  The phone may have these open (0214). A day with no such row says nothing in `missing`.

## Alternatives rejected

- **Fold in the Mac step and ship items** — the doing lane and the writer would see only what a
  threshold kept, and a threshold changed later could not be applied to the past. Rows per record
  cost 40,870 rows and about 23 MB for everything his Mac holds, which one backfill carries.
- **Put the records in `device-apps`** — that source has one switch and a four-week backfill of
  stretches; the records reach back a year and need no pairing, and turning off the stretches
  (`mac_ship.device_apps`) would also turn off the messages.
- **Drop the long text field** — it is what he saw or heard (a post, a lyric line); he asked for
  everything. Only 9 of 19,071 now-playing rows on his Mac exceed the cap.

## Consequences

- The brain holds who he wrote to or called, in which app and when, in clear. There is no message
  text anywhere in these streams; the long text field holds the posts and lyrics he met, kept
  because he asked for everything. Erasing all data removes them with the imports directory.
- Retention is the Mac's: four weeks of playing and Bluetooth, a year of Siri. What the Mac drops
  before a read is lost, so the shipper has to run.
- Each device's records are as late as 0304's; the day line's `screen_synced_ms` is the only
  statement of how far a device is known, and the new kinds do not add one.
- Whose chat is "the conversation" is Siri's own naming, which in a group or an app's channel is
  not a person; the day line shows that name, then the other person's, then a handle, and can be
  wrong for a message to one's self or a notification Siri logged as a message.
- The order of the Bluetooth battery levels is unconfirmed; they are stored and drawn in the
  stream's field order.
- The helper binary is rebuilt, and signed with the same identity and bundle id as before so the
  Full Disk Access grant still applies.
