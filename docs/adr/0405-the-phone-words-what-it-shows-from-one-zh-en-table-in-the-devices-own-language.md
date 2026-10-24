# ADR 0405 — The phone words what it shows from one zh/en table, in the device's own language

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** 0327

## Context

- ADR 0327 put every fixed string the phone shows in one zh/en table, held by a canary, in the
  language the brain's `language` setting names (`GET /inherent/language`), else the phone's.
- 2026-10-23 23:27, allen's iPad (system language English) opened the first iPad build: its tab
  bar read Today / Chat / Map / Settings while the screens around it, still `// words: legacy`,
  were Chinese, and the brain's row lines were English. One device, three sources of language.
- allen, 2026-10-24: 「英文就英文…都做双语版本的，然后就按照系统来变吧」 — the app's words follow
  the device's system language.
- iOS restarts an app when its system language changes, and each device has its own (his iPhone
  and his iPad need not agree). The watch has no settings of its own for Jarvis; the phone sends
  it the language with each day (`WatchDay.language`).

## Decision

Word every fixed string the phone shows from one table of its own, each entry with its zh and
en side by side, in the device's first preferred language (zh when it is Chinese, else en), and
hold Chinese literals in the app's Swift sources to that table and to the files that hold them
today, whose number only goes down.

- The table is `ios/Jarvis/App/Words.swift` and its `Words+<area>.swift` siblings, each entry
  built from both languages so a missing one does not compile; a sentence with values is a
  function of them.
- The phone and the iPad read their own system language at launch and ask the brain for none.
  The watch takes the phone's from the day it is sent and keeps it for its next launch.
- The canary and its marks (`// words: legacy`, `his`, `sample`) stay as ADR 0327 set them.

## Alternatives rejected

- **The brain's `language` setting (ADR 0327)** — on 10-23 it gave an iPad set to English a
  table language that the app learned only after its first screen was drawn, and a device
  cannot be English while the Mac is Chinese; allen chose the device's language on 10-24.
- **Apple's String Catalog (`Localizable.xcstrings`)** — it would now follow the right
  language, but a missing translation still falls back to its key without failing anything,
  while a table entry without its en side does not compile.

## Consequences

- The Mac, the brain's spoken words and the row lines her writer makes still follow the brain's
  setting, so a device in the other language shows the brain's words in that language beside its
  own fixed words.
- The phone's 回答语言 row and `GET /inherent/language` no longer decide anything on the phone.
- Until a file moves into the table, its words stay Chinese on an English device.
