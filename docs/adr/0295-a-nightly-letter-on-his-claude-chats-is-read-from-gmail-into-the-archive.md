# ADR 0295 — A nightly letter on his Claude chats is read from Gmail into the archive

**Status:** Accepted
**Date:** 2026-10-18
**Supersedes:** none

## Context

- ADR 0259 keeps what he hands over raw and reads it into `import_entries`. His claude.ai chats
  came in as a one-off export; they go on every day, and the export is a file he has to download.
- On 2026-10-18 he set up a scheduled task in Claude (Cowork, in the cloud) summarize each
  day's chats every night. A cloud task cannot reach his tailnet, so it cannot call the brain. It
  can write to his Gmail through the claude.ai Gmail connector: most likely as a draft, possibly as
  a mail he sends to himself.
- The brain already reads his Gmail with `gmail_search` and `gmail_get` only (ADR 0055, 0155,
  0274). Both mail pollers list with `-in:drafts` (and the commitment one `-in:sent`), so a draft
  never reaches them; a letter that did would be typed as job mail or asked about as an appointment
  ("Friday 2 PM" in a summary of a chat about an interview).
- A re-run of the task, or an edit of the draft, writes the same day again with other words. A
  draft edited through the Gmail API becomes a new message with a new id.
- He keeps every field of what he collects: a source he later finds too thin must already be on
  disk.

## Decision

The brain polls Gmail for the letter and keeps every revision of it, raw and read.

- **The letter.** Subject `Jarvis Claude 日报 <YYYY-MM-DD>`, body Markdown that he can read too:

  ```
  jarvis-claude-daily: 1
  date: 2026-10-18
  timezone: America/Vancouver
  chats: 3

  ## <chat title>
  - id: <the chat's claude.ai id or URL, if the task can see it>
  - project: <project name, or ->
  - time: 09:12-10:40
  - messages: 14
  - summary: <what the chat was about and where it ended>
  - decisions: <decided things, or ->
  - todos: <things he said he will do, or ->
  - mentions: <people, places, products, dates named, or ->
  ```

  Any other `- key: value` line is kept in the entry's fields as written. It is read leniently: a
  dash, en dash or `to` in the time, a missing line, a missing header.
- **The poll.** Off by default (`claude_daily:` in `config/jarvis.yaml`); every `poll_s` it lists
  mail since `backfill_since` for the subject phrase `Jarvis Claude` twice, with Gmail's default
  search (sent and received copies) and in the drafts, and takes the ones whose subject starts with the title
  above. The phrase is a net; the subject is checked in code.
- **Kept.** For each such message not kept yet it keeps both Gmail answers (metadata and full)
  raw under `imports/claude_daily/` named by the message id (ADR 0259), and reads the body into
  `import_entries` with source `claude_daily`: one `daily` entry for the whole letter and one
  `daily_chat` entry per `##` section, both stamped in the letter's own zone. The raw file is
  written after the entries are committed, so a crash between the two is mended on the next cycle
  and a letter is never read twice.
- **Revisions.** An entry's key holds the day, the chat's id (else a digest of its title) and a
  hash of its text with spacing ignored, so a draft and its sent copy are one revision, an edit
  adds only the chats that changed, and nothing is overwritten. Of the day entries of one day, the
  one added last stands for the day.
- **No letter is dropped.** One with no `##` section, or none of the header, is still kept raw and
  read as one whole-day entry marked unparsed. A failure is logged by message id and kind, never
  by content.
- **Readers.** The `archive` tool takes the source `claude_daily`; the day timeline gets one item
  a day, kind `claude_day` (a span over the chats' times, their titles as text), from the newest
  revision.
- **Not mail to triage.** The job-mail and commitment pollers set a message with this subject
  aside by that subject alone: it is neither typed, asked about, nor read for its body.

## Alternatives rejected

- **The cloud task posts to the brain.** The brain is on the tailnet only (ADR 0170); a cloud task
  has no route to it, and opening one to the internet puts an endpoint in front of his memory for
  a nightly summary.
- **Poll only the inbox, so a draft is not needed.** The connector's usual write is a draft, and
  a draft never lands in the inbox; the task would have to send mail, which a first version of it
  may not be allowed to do.
- **Key a letter by its Gmail message id.** A draft and its sent copy are two ids with the same
  words; the day would be read twice, and a re-run would look like a new day instead of a revision.
- **Overwrite the day's entries on a revision.** ADR 0259 never rewrites a held entry, and an
  earlier wording he reads back later is the point of keeping them.

## Consequences

- A letter edited in place under the same message id is not read again. Gmail gives an edited
  draft a new id; this is not yet checked against the connector.
- A letter whose body is HTML-only is read as text with its markup stripped; headings that arrive
  as `<h2>` are not `##` and the letter is then read as unparsed.
- Every revision of a day stays in `import_entries` and shows in `archive` searches; only the day
  timeline shows the newest.
- The chats' summaries pass through Gmail and Anthropic as they would anyway; the brain asks no
  model about them.
