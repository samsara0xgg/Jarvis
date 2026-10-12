# ADR 0255 — A picture he sends stays in the conversation as a picture, and his devices can open it again

**Status:** Accepted
**Date:** 2026-10-12
**Supersedes:** 0223

## Context

- ADR 0223 gives the conversation model a picture only in the turn it arrives with. Later
  turns get a marker line (`[attached: image NAME]`) and, ten minutes on, a note in words.
  Allen rejected that on 2026-10-12. He wants pictures kept the way mainstream chat assistants
  keep them: the picture itself, not a description of it. A description drops whatever its
  writer did not think to mention, and that is exactly what he asks about later.
- 0223 rejected pictures in later turns for two reasons. Both came from the GPT preset that
  was the default then:
  - the words-only history keeps the cached prefix stable;
  - a 4 MiB picture costs about 5.6 MB of base64 on every later request.
- ADR 0253 makes Claude the default (Sonnet 5.5 for conversation and vision). Three facts
  change the picture:
  - On Claude a picture in the prompt costs about 1,600 tokens at the long edge the phone
    already sends (1568 px, ios `AttachUpload.swift`).
  - Cached reads cost a tenth of input (Sonnet 5.5: $2 per 1M input, $0.20 per 1M cache
    read). A picture that stays in context for 50 later turns costs about $0.02 in all.
  - Anthropic's Files API takes an image once and lets later requests refer to it by id. The
    request carries an id, not the bytes. The Pi uploads over a home connection, and 4 MB of
    base64 per turn would add seconds to every voice turn.
- The cached prefix stays stable as long as a picture's block is byte-identical from turn to
  turn. A file id gives that.
- History today is every record since the day-summary boundary, up to 40,000 characters of
  text (`session.context: day_summaries`, ADR 0044). Anthropic accepts up to 100 images per
  request.
- Files are kept 30 days (`attachments.retention_days`). Allen confirmed 30 days on
  2026-10-12 and approved this design the same day.
- The phone shows a picture he sent only from an in-memory copy that is lost when the app
  closes. Rows from `GET /inherent/conversation` carry no attachment ids. Their text ends with
  the marker line, and an attachment-only turn starts with the filler `t("attach.no_words")`.
  Both show in his bubble. No route returns a picture's bytes.

## Decision

A picture he sends stays in the conversation as an image while its turn is in the history
window and its file is kept. His devices can list it with his line and open it again. Limits:

- **Claude presets only.** On a preset whose provider is `anthropic`, the history message
  of his turn carries its pictures as image blocks after his words, in the same place the
  live turn had them. Other providers keep 0223's marker line.
- **By file id.** A picture is uploaded to the Files API in the background as soon as it is
  saved, and one saved earlier is uploaded at start or the first time a request needs it.
  Its file id is kept next to the file, and every later request refers to it by that id.
  The live turn still sends the bytes inline, so a new picture never waits on an upload.
- **The 20 newest.** At most the 20 newest pictures in the window ride as images. An older
  one, or one whose file is gone, falls back to its marker line and its note.
- **The note becomes an index.** The note is still written as in 0223. It feeds search, the
  day line, the day summary and the ledger. It enters the conversation only when its picture
  does not ride as an image. `look_at_picture` stays for pictures past the 20 or out of the
  window.
- **The file and its copy share a life.** When the local file is swept after 30 days, or
  erased, its Files API copy is deleted with it.
- **Devices see pictures, not markers.** Each of his rows from `GET /inherent/conversation`
  carries `attachments: [{id, kind, name, gone}]`. Its `text` is his words alone: no marker
  line, and no filler on an attachment-only turn. `GET /inherent/attachments/{id}` returns the
  stored bytes to a paired device while the file is kept.
- **The phone opens its own copy first.** The phone keeps the full-size original of what it
  sent, keyed by attachment id, and opens that on tap with zoom. It fetches the stored copy
  only when its own is gone.
- **Unchanged from 0223:** the attachment-only reply language, the kinds and size limits,
  the 512 MiB store, export and erase.

## Alternatives rejected

- **Base64 in every later request.** Twenty phone pictures (about 400 KB each) come to about
  11 MB of base64 per request. That is several seconds of Pi upload on every turn, voice
  included. A file id is under 100 bytes.
- **Keep 0223 (words, plus a tool to look again).** The 2026-10-11 live case failed exactly
  here: a later question about the photo got an answer from the note. A second look only
  happens when the model thinks to call the tool. Allen asked for the picture itself.
- **No cap.** Anthropic accepts 100 images per request. At about 1,600 tokens each, a
  50-picture day puts 80,000 tokens into every turn, and each cache write costs 1.25 times
  input. Twenty keeps the worst case near 32,000 tokens.
- **Downscale on the brain.** The phone already sends 1568 px JPEGs. The Pi has no image
  library in its dependencies, and shares arrive through the same phone code.

## Consequences

- Pictures now leave the house twice: once inline in the live turn, and once to the Files
  API, where they stay up to 30 days. `docs/spec.html#egress-table` gets that row.
- A picture's upload starts when it is saved, so the next turn normally finds its id; a render
  waits for uploads still running at most 5 seconds in all, and a picture without an id by then,
  or whose upload failed, rides as its marker and note for that request. A request the API
  rejects over a file id it cannot read is sent once more the same way, and that id is dropped.
- Moving the window boundary, or a picture dropping out of the 20, changes the prefix once.
  That costs one cache write.
- Switching to a non-Claude preset silently loses the pictures in history.
- The Mac app (`desktop/resonance`) must read the new row fields to show pictures. Until it
  does, his attachment-only rows there show empty text.
