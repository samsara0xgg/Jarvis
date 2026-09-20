# ADR 0022 — Screen capture lives inside TimeSink

**Status:** Accepted
**Date:** 2026-09-20
**Supersedes:** none

## Context

- Allen wants Jarvis to know what he was doing at a given time, including the
  content of the window in front of him, not only which app was open. ADR 0021
  reused TimeSink for app spans and explicitly kept screen capture outside that
  integration; that clause is what this decision replaces.
- Every input a screen collector needs already exists in TimeSink: a 1 s tick,
  the AX focused window, idle seconds, and lock/sleep/unlock signals. The
  daily-loop handoff forbids a second app/idle tracker because two clocks
  disagree about span boundaries.
- Screen Recording is a TCC grant per app bundle. Jarvis's Python interpreter
  holds one for `screen_look`; TimeSink did not, so the collector's home also
  decides who asks for the permission.
- Allen chose the simplest first version on 2026-09-20: keep screenshots in
  meetings, exclude only password managers and Keychain Access, keep images
  7 days, pause only from the menu bar, and reduce the stability rules to
  three numbers (3 s settle, 30 s re-check, 10% of thumbnail cells changed).

## Decision

TimeSink owns screen acquisition end to end (window identity, stability
schedule, window-only screenshot, on-device OCR, dedupe, retention, exclusions,
pause, and the state reasons idle/lock/sleep/pause/start/stop); Jarvis reads
its `capture` and `stateEvent` tables through the existing read-only adapter as
the `screen` source of query_activity and never captures, summarizes, or sends
an image itself. No model call and no network leave TimeSink for this feature.

## Alternatives rejected

- **Collector in the Jarvis daemon (PyObjC)** — needs its own foreground and
  idle sampling, i.e. the second tracker ADR 0021 already rejected; a
  per-second AX read plus screenshots in Python costs more CPU than the Swift
  tick that already runs, and the two processes' clocks would file captures
  under the wrong span.
- **TimeSink emits window-stable events, Jarvis captures** — both processes
  need Screen Recording, capture timing crosses a process boundary, and the
  post-capture window recheck (a frame of A must never be filed under B) would
  have to round-trip; measured AX reads already take up to 0.25 s each.
- **Standalone Swift helper** — a third process, a third TCC grant and a third
  SQLite file for the same signals TimeSink already samples.

## Consequences

TimeSink becomes a local activity collector, not just a timer: it holds
Screen Recording, a rolling image folder of a few hundred MB, and plaintext
OCR of whatever was on screen (only the app-level exclusion list and the 7-day
image expiry limit what is retained). Jarvis depends on TimeSink's `capture`
schema and reports screen coverage=unavailable on older stores. The first OCR
after TimeSink launches warms up for roughly 20 s. Captures made before a span's
first 30 s heartbeat carry span_id=null and must be joined by time overlap.
