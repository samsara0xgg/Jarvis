# ADR 0109 — Startrail speaks Jarvis's language

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- Everything Startrail (the Agents window, ADR 0073) shows was written in
  Chinese. An English-only build of it existed as a demo: it replaced the
  Chinese in 45 files (1,037 lines), so it could not be merged without taking
  the Chinese away.
- Allen, 2026-10-01: 「既然做了英文版本，能不能把英文版本也推到我主版本？如果说
  我主语言是英文的话，StarTrial 也会变成英文，全英文可以吗？」
- Jarvis already has one language: `language` in settings.yaml, which the
  daemon serves on GET `/inherent/language` and switches on POST. The desktop
  settings switch changes it, and the companion's own panel already follows
  it.
- Startrail's words come from two processes. The agent host writes row lines,
  state labels, answers to requests, notes and errors; the page writes the
  rest. A string a module builds when it loads is fixed before any language is
  known.
- The page matches some of what the host wrote back by pattern: a request's
  done label (拒绝 / 没回答), the refusal that mentions uncommitted changes,
  the exported conversation's "## 你" heading, the names of the logs. A
  conversation keeps what the host wrote when it wrote it, so one record can
  be in the other language when it is read.
- The agents' replies and Allen's messages are not Startrail's words.

## Decision

Startrail's own words follow Jarvis's language: English when the daemon says
`en`, Chinese when it says anything else or does not answer.

Its limits:

- The host asks the daemon at start and whenever a window asks it on `/lang`;
  while the daemon is away, the last answer stands. The page asks `/lang` once,
  before any other module draws. The companion process, which owns the app menu
  and the Mac notifications, asks the host when the window opens and once a
  minute. A switch reaches an open window when it loads next.
- Both languages stand together at each use, as `tr('中文', 'English')`. There
  is no catalog and no library. The host builds nothing at load with it: a
  constant that needs it is a function.
- English runs longer, so its layout differences are `:lang(en)` rules, and
  Chinese keeps the layout it had.
- Only Startrail's words are translated. What the agents wrote and what Allen
  wrote are shown as they are.
- Every pattern the page runs over text the host wrote accepts both
  languages.

## Alternatives rejected

- **A catalog of keys, or an i18n library.** About 1,600 `tr` sites in
  the host and page files would move into a second file, away from the code whose behaviour they
  describe. The companion's own panel already keeps its two languages side by
  side in the code (`companionSettings.ts`).
- **Keep the English-only build as a branch.** It rewrites 1,037 lines of the
  files Startrail's own work touches most, so every later change to the
  Chinese conflicts with it.
- **A language setting of Startrail's own, or the system language.** A second
  switch that can disagree with the one Allen already has and asked to follow.
- **The page reads the daemon itself.** The window holds the host's key and
  not the daemon's, and the host already reaches the daemon.

## Consequences

- Every new string in Startrail is written twice, and read twice in review.
- Both arms of a `tr` are evaluated in either language, so an expression in
  the arm not shown must still not throw.
- A record the host wrote stays in the language it was written in: a done
  label, a note or a summary can be the other language next to new ones.
- A window that is already open keeps its language until it is opened again; a
  host whose daemon was away at start and at every window open speaks Chinese.
- Anything that greps compiled output for a Chinese literal sees
  `tr('中文', 'English')` instead.
