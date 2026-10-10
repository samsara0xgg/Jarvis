# ADR 0212 — "Here" is where the device he spoke from is

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** 0198

## Context

- ADR 0198 made "here" the phone's latest report, but only on a brain, and ADR 0194 made it the
  Mac's own fix everywhere else. ADR 0207 then let a phone talk to a Mac running alone, so one host
  now answers turns from both devices.
- When Allen is out with the phone and the Mac is at home, the Mac's fix answers "where am I" and
  starts a bus trip from the house. When he is at the Mac, the phone's report is up to about
  500 m old and the Mac's fix is fresh.
- Which device a turn came from is already in the log: a paired device's turn opens with a row
  written under the device's name, and the Mac's own turns under `mac`. Words sent to `submit`,
  `ask` or `share` were written under `mac` whoever sent them, so they could not be told apart.
- The reasons for reading the phone's report without caching it, and the rules for its age, are
  ADR 0198's and stay as they were: the phone reports on a stay or a move of about 500 m, so an
  old report means he has not moved far.

## Decision

Answer "here" from the device the turn came from: the phone's latest report for a turn a paired
phone opened, this Mac's fresh fix for a turn of the Mac's own.

Its limits:

- **A turn's origin is the name its opening row was written under.** A Mac running alone has no
  terminal, so a paired device that spoke to it is a phone. A turn with no such row is the Mac's.
  The HTTP routes a phone uses write that name too.
- **A brain is unchanged.** It has no Mac to read, so every turn gets the phone's report.
- **The phone's reading follows ADR 0198.**
  - Nothing is cached: the log is read at each call.
  - The answer carries the report's age, its accuracy and that it is the phone's.
  - There is no age cutoff, and the model is told that an old report means he has not moved far.
  - A visit counts at its latest known time.
- **A phone turn before any report falls back to the Mac's fix.** The answer says no phone has
  reported yet, so the model does not take it for where he is. With no Mac to read, it is the
  tool error that tells the model to ask him.
- **The tool descriptions name both sources** where a host has both, and only the one it has
  otherwise.

## Alternatives rejected

- **Prefer the phone whenever it has reported.** At the Mac with a phone report from an hour ago,
  the answer would be up to 500 m off while a fresh fix costs under 2 s (ADR 0194).
- **Prefer whichever reading is newer.** A Mac fix is always newer than the phone's last report,
  so the phone would never win, and the closed Mac at home would still answer for him.
- **Two tools, one per device.** The model would have to know which device he is on, and it has
  only the turn's words to go by.
- **Ask the phone for a fresh fix.** ADR 0197 gives the host no way to wake the phone, and a push
  takes seconds at the system's discretion.

## Consequences

- A device named `mac` cannot be told from the Mac's own rows.
- Words a phone sends over `submit`, `ask` or `share` are now written under its name. The phone's
  row stream and its barge-in follow turns by that name (ADR 0209), so they now cover these turns.
- If the phone stops reporting, a phone turn answers from the last report and only its age shows
  it, as on a brain. A phone turn with no report at all answers with the Mac's fix, which may be
  at home.
