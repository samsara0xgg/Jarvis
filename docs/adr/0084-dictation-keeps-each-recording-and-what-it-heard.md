# ADR 0084 — Dictation Keeps Each Recording and What It Heard

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- Jarvis's dictation (ADR 0077) wrote nothing but its spend: no audio, no
  words, not even a log line per session. When a dictation came out wrong
  there was nothing to replay, and no way to tell the recognizer's mistake
  from the polish's.
- On 2026-09-29 Allen asked for the raw audio of at least the last 20
  dictations, or all of them if the volume is small, and a proper log,
  "毕竟我们现在在实验阶段，每一段语音记录都很重要".
- Voice turns already keep their audio when `memory.retain_audio` is on
  (Allen's settings: on; shipped default: off), as 32 kb/s AAC through macOS's
  `afconvert` in `memory/audio`, and ADR 0067 deletes files there by age
  (30 days by default), exports them and clears them. That sweep reads only
  the files directly in the folder.
- Allen's dictation volume, from Typlus's trace (17 days): median 1,325 s of
  audio a day, 5,543 s on the heaviest day. At 4 kB/s that is ~5 MB a day,
  ~22 MB at most, ~160 to ~660 MB over 30 days.
- ADR 0067: logs say what happened, never what was said.

## Decision

When recordings are kept, every dictation session, finished, empty, failed or
cancelled, leaves its audio in the recordings folder as
`dictation-<local time>.m4a` with a same-named JSON note of when it started,
how long it ran, the app and window, how many stretches it was heard in, how
long hearing and polishing took, how it ended, and the raw and polished words;
both follow the recordings' retention, export and clear. Every session also
writes one daemon log line of the same facts without the words.

## Alternatives rejected

- **The last 20 recordings, like Typlus's `keep_recordings`** — at ~5 MB a
  day, 30 days of everything costs less than a gigabyte, and a count would
  drop a morning's dictations by the afternoon of a busy day (222 in one day
  in Typlus's trace).
- **Words in the daemon log** — ADR 0067 cut them out of the logs, which are
  kept outside the recordings' retention, export and clear.
- **A `dictation/` subfolder** — the retention sweep and the clear button read
  only the files directly in the recordings folder, so it would never expire.
- **WAV** — 32 kB/s, eight times the AAC the voice turns already use.

## Consequences

- With `memory.retain_audio` off (the shipped default) nothing is kept, and
  the log line is the only trace.
- The words of every dictation sit on disk for the retention period, beside
  the audio, and leave with an export.
- Saving runs on the dictation's hearing worker after the result, so a new
  session started at once waits for the AAC transcode (0.07 s for a minute
  of audio, measured) before its first stretch is heard.
