# ADR 0309 — The Mac's Photos, Messages and Safari history are kept on the brain as one row per record

**Status:** Accepted
**Date:** 2026-10-20
**Supersedes:** none

## Context

- Allen asked for every piece of data his devices give to be stored on the brain, raw and clean,
  for later layers to analyse; ingestion interprets nothing. ADR 0305 did that for the streams
  macOS syncs into Biome. Three more reach the Mac as databases: iCloud Photos puts his iPhone's
  photos in `Photos.sqlite`, Messages keeps iMessage, SMS and RCS in `chat.db`, and Safari's
  `History.db` holds the pages he opened on the Mac and, synced, on his other devices: 339 of
  its 6,392 visits have `origin` 0 and 6,053 have 1. He approved storing the text of his messages.
- The words of a message are mostly not in `message.text`: of his 2,038 messages, 1,888 have it
  NULL and keep the words only in `attributedBody`, an archived NSAttributedString (a
  typedstream); 145 have `text`, 5 have neither.
- The databases are TCC-protected, so the 0304 helper's Full Disk Access grant is the one that
  applies. All three are in WAL mode and owned by running apps, so a reader must neither block
  them nor miss the rows still in the `-wal` file.
- Unlike 0305's records, these rows change after the fact: a message is read, delivered or edited,
  a photo is favourited, edited or trashed, and iCloud brings old messages and photos down late,
  with their old dates. 0305's key puts a few digits of the fields before the digest, so a changed
  record would become a second row beside the first. Of his messages, 1,133 carry a read time,
  which is written after the message.

## Decision

Read the three databases with the 0304 helper and ship each record as one row under
`device-records`: kinds `photo`, `chat_message` and `safari_visit`, stored and not interpreted.

- A row is a record with its fields under their own names. Photos are metadata only, never image
  bytes. Message text is kept whole up to 4 KB, as in 0305, then cut and marked, and so are a
  visit's address and title; attachments are their names, types and sizes. The words are `text`,
  else the string decoded from `attributedBody`; a blob that does not decode gives no text, never
  a guess. Codes Apple defines (a photo's subtype, a visit's origin) are stored as numbers, with
  only the flags that are documented API beside them (`live` is playback style 3, `screenshot`,
  `burst`).
- The key is the record's own identity, then the content digest 0294 requires: the asset uuid,
  the message guid, a visit's time and the file's own id for it. A change is therefore the same
  row at a newer revision. Photos and messages are not told apart by a digest of their fields.
- Reads open the file read-only, never immutable and never copied, with a short busy timeout and
  one read transaction. A database that cannot be read (no grant, no such file) is named in the
  helper's answer and logged once; the other kinds still ship.
- The cursor is the newest time a record was made, changed or read (for a photo also added,
  adjusted or trashed). A read starts 3 days before the oldest of these cursors and never more
  than 14 days back, and the three databases are read whole once a day, which is what finds
  late-arriving and late-changed rows. The first read, and the first after a database is met,
  takes everything. A row whose content the brain already holds is not sent again. Cursors and
  row memory move only after the brain kept the batch, and a first pass goes in batches under the
  16 MiB route limit.
- Nothing is drawn on the day line. The doing lane or the day's writer may read these rows later.

## Alternatives rejected

- **Key by a digest of the fields, as 0305 does** — a read receipt would add a second photo or
  message row beside the first, and the brain's newest-revision rule would never merge them; 1,133
  of his 2,038 messages would be two rows.
- **Open the files immutable, or copy them first** — an immutable open skips the `-wal` file,
  where the newest rows live until a checkpoint, so the last messages and photos would be missing;
  a copy of a live WAL database is torn unless its `-wal` and `-shm` are taken in the same
  instant.
- **Read everything every pass** — his three databases are 12,540 rows and 13.8 MB of JSON, 1.7
  seconds of helper time; every 15 minutes that grows with years of messages, not with what
  changed. The window keeps a pass at what changed and the daily whole read bounds what it can miss.
- **Decode `attributedBody` with `NSUnarchiver`** — it raises Objective-C exceptions that Swift
  cannot catch, so one odd blob would end the whole read. Reading the string after `NSString` in
  the typedstream matched `text` on 141 of the 145 messages that have both; the other 4 differ
  only by the attachment character `U+FFFC` the blob leads with and `text` leaves out.

## Consequences

- The brain holds, in clear, every message he sent or received with its sender, group members and
  attachments' names, where and when each photo was taken down to the street, and every page he
  or his other devices opened. Erasing all data removes it with the imports directory.
- Only the words of a message as macOS holds them now: an edited message keeps its latest text, a
  message deleted on the Mac stays on the brain (the store is append-only), and a message not yet
  downloaded by Messages in iCloud is not in `chat.db` to be read.
- A row changed after the daily whole read waits for the next pass to be a new revision; a row
  changed only outside the 14-day window waits up to a day.
- Photo counters Apple keeps changing (views, plays, machine scores) are not read, so that an
  asset is not a new revision every time Photos looks at it.
- `origin` is stored as Apple has it. That 0 is a visit made on this Mac and 1 one synced from
  another device is inferred from the counts above and not confirmed, and no flag of a screen
  recording was found beyond the screenshot flag on a video.
- Only the system Photos library at `~/Pictures/Photos Library.photoslibrary` is read.
