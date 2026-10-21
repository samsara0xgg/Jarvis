# ADR 0325 — The phone words what it shows from one zh/en table, in the language the brain's settings name

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- yilun's rule (2026-10-19 21:26Z): prompts for models are English; every fixed string Jarvis
  says or shows lives in the zh/en table, in the language his settings name (the system's by
  default); a check fails on Chinese outside the table.
- The brain keeps it: `jarvis/shared/lang.py` and the canary
  `tests/canary/test_canary_cjk_only_in_lang_table.py`. The Mac's agents window speaks the
  daemon's language (ADR 0109). The brain answers `GET /inherent/language` with
  `{"language": "zh" | "en"}` to any admitted caller.
- The phone has neither: on 2026-10-20, 87 of the 144 Swift files in `ios/` (the app, its widgets and its share extension)
  hold Chinese string literals, `ios/project.yml` names zh-Hans its development language, and the 回答语言 row of
  its settings edits a value nothing reads.
- The phone shows words before it is paired and while the brain cannot be reached
  (「还没连上大脑…」).
- 我的足迹 (ADR 0323) is the first new phone page since the rule. The UI audit's 精修版 will
  rewrite many of the phone's existing words.

## Decision

Word every fixed string the phone shows from one table of its own, each entry with its zh and
en side by side, in the language the brain's `language` setting names, else the phone's system
language; and hold Chinese literals in the app's Swift sources to that table and to the files
that hold them today, a list that only shrinks.

- The table is `ios/Jarvis/App/Words.swift` and its `Words+<area>.swift` siblings: each entry a
  typed static or function built from both languages, so a missing language does not compile.
  A sentence with values is a function of them.
- The language is read from `GET /inherent/language` when the phone is paired, at launch and on
  each return to the foreground, and kept for the next launch; before the first read it is the
  phone's first preferred language (zh when it is Chinese, else en). A change redraws the screen.
- A canary in the Python suite lists every Swift string literal holding CJK under `ios/Jarvis`, `ios/JarvisWidgets` and `ios/JarvisShare`
  outside the table and fails on one in a file not on its legacy list, and on a listed file
  that holds none any more. Code that reads his own words (the recognizer's zh lane, word lists)
  is listed by file and name, as the brain's matchers are.

## Alternatives rejected

- **Apple's String Catalog (`Localizable.xcstrings`)** — its language is the app's iOS
  language, set in the phone's Settings, not the setting he switches on the Mac; following the
  brain means loading an `.lproj` bundle by hand, and a missing translation falls back to its
  key without failing anything.
- **Words served by the brain from `lang.py`** — the phone needs its words before pairing and
  while the brain is unreachable.
- **`tr(zh, en)` at each call site, as the agents window does** — one screen's words scatter
  over its files, and the check would have to parse call arguments to tell a pair from a lone
  Chinese literal.
- **Move all 81 files now** — every screen changes at once while the 精修版 rewrites the same
  words.

## Consequences

- Until a file moves into the table, its words stay Chinese in English, so one screen can mix
  the two languages.
- A new Swift file with a Chinese literal turns the Python suite red, which CI runs on every
  push to main; iOS work that lands without running it learns there.
- Moving a file into the table means taking it off the legacy list in the same commit.
