# ADR 0161 — The moment is read from TimeSink when an alert is decided or shown

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04: Jarvis should know what he is doing when it decides to
  reach him, and let TimeSink (already running, ADR 0021) supply it: no call
  or meeting interrupted by a card or a spoken line, nothing shown to an empty
  chair, how long a company's job pages held him, and a few facts saved with
  every decision so a later judge can learn from them (ADR 0155, 0157).
- ADR 0023 keeps observers silent and model-free and prefers reading at the
  time of need over a background loop. ADR 0156 keeps unprompted sound on
  private output; 0153 owns the quiet level; 0158 and 0159 own the summary
  that held alerts become.
- TimeSink's own store, read read-only on 2026-10-04 (75,000 spans since
  2026-08-24):
  - A `span` row is an app, an optional Chrome `domain`/`url`, a window `title`
    and a start and end. Heartbeats extend the open row every few seconds, so
    its end is the last moment seen. A `deviceID` marks a span synced from
    another machine.
  - A `stateEvent` row is a time and a kind: `idle`/`active`, `lock`/`unlock`,
    `sleep`/`wake`, `start`/`stop` (the app itself) and
    `tracking_pause`/`tracking_resume`. On `idle` and `lock` the open span is
    cut at exactly the event's time, so "a span ended after the mark" means he
    came back. ADR 0021 recorded that reasons were not persisted; the store has
    since gained this table.
  - **Not recorded:** whether a call is running, screen sharing, microphone or
    camera use, collector health, or any title when the app gives none. The one
    signal of a call is the front window: Zoom's meeting windows are titled
    "Zoom Meeting", a floating video window, "Participants (N)" and "Menu
    window", and its lobby "Zoom Workplace", "Settings", "Login"; Teams' web
    join page is "Meeting join | ..."; Tencent Meeting's lobby is
    "腾讯会议" and its meeting window has an empty title. No Meet, FaceTime or
    Webex row exists in his store; their patterns (a Meet code in the URL, a
    FaceTime window named for the caller, Webex's meeting-manager bundle) come
    from how those apps behave and are unverified here. Discord voice does not
    change its window title, so it cannot be seen.
  - A call is seen only while its window is the front one, or was within the
    last 90 seconds. A call in a background window is a false negative.
- Titles and URLs are private material. A model may be shown app names and
  site domains; the doc and snapshots here carry no title and no URL.

## Decision

Read TimeSink when an alert is decided, spoken or served, never in a
background loop, and let that read hold every card and sound while he is in a
call or away, put the same read in every decision snapshot as a short
locally built 现况 doc, and show on the job ledger how long each company's
pages held him.

Its limits:

- **Facts.** One read-only transaction gives the front app, its site domain and
  when that app-and-site stretch began; present, idle, locked or asleep; in a
  call, with which app; today's job-site seconds and top apps. Screen sharing
  is always `unknown`. Idle, lock and sleep come from TimeSink's own marks and
  are trusted only when no span ended after them, TimeSink has not stopped or
  restarted since, and the mark is under eight hours old. Every field may be
  `unknown`. A store that is off, missing or unreadable, or whose newest span is
  over 120 seconds old with no such mark, makes all of them `unknown`. While
  idle, a call window just before it still counts as a call.
- **Hold.** A known call, or a known idle, locked or asleep state, holds all
  alerts: none is served to the client or marked shown, the spoken line does
  not start, and each stays pending. At its end the existing rules of ADR 0158
  apply: two or more that waited are one summary, one alone is its own card.
  Unknown holds nothing, so behaviour is exactly as before. ADR 0156's output
  guard is untouched.
- **现况 doc.** Built by code, no model, at most 400 characters, in English
  because it is text for models: Now (front app, site domain, since when, in a
  call, screen share, presence), Today so far (job-site time and the top apps by
  time) and Your situation (his watch list, goals and rules: empty for now). Each of nine fields is switched on or
  off in the `moment.fields` config block, checked at boot, because which facts
  help is to be learned from how he rates the cards. The doc and its structured
  fields are stored with every decision snapshot (`job_decision.moment_json`,
  the context pack's `situation.moment`) so it can be replayed. The generic
  per-card snapshot written in parallel (decision record 0160) must call
  the same `Moment.snapshot`.
- **Job time.** A span is job-site time on a LinkedIn `/jobs` path, an
  applicant-tracking host (Workday, Greenhouse, Lever, iCIMS, SmartRecruiters,
  Ashby), or a career host or path of a company in the ledger, derived from the
  base domain of that company's mail. ATS and LinkedIn spans name a company when
  its name is in the title or in the host or path; otherwise they are
  `other job sites`. Seconds per company per local day over 14 days are computed
  when `GET /inherent/jobs` is read and only returned: TimeSink data is not
  stored. Titles and URLs are read to attribute and never returned.
- **Egress.** None. TimeSink is read locally, read-only; no model sees a row.

## Alternatives rejected

- **Poll TimeSink every few seconds and keep the latest facts** — the 5 s
  client poll already reads `/inherent/notices`, so a read there costs at most
  one query per five seconds (cached 5 s) and nothing runs while no alert exists;
  ADR 0023 already declined a background loop for this data.
- **Use TimeSink's idle mark alone for "in a call"** — listening in a meeting
  takes no input, so idle would hold alerts only until he next moves the mouse
  and then release them mid-call; the window just before the idle mark is what
  says a call was running.
- **Hold only speech and sounds, still show cards** — a card on a shared
  screen during a call is the interruption he named.
- **Store the facts and the job time in memory.db** — retention would copy
  another app's data for no reader; the facts are cheap to read again and a
  snapshot keeps the one instant it needs.
- **Let a model read raw spans and write the doc** — titles and URLs would
  leave the machine and cost per card; a template over typed fields says the
  same in 400 characters.

## Consequences

- A call whose window is behind others for over 90 seconds, a Discord call,
  and screen sharing are not seen, so alerts can still reach him there.
- A lobby-titled Zoom window, a Teams chat titled "Call ..." and other title
  look-alikes can hold or release wrongly; the patterns are a table in
  `jarvis/state/timesink_moment.py` to extend as real rows show them.
- If TimeSink dies while he is locked, alerts wait up to eight hours for a mark
  that will not be undone, then flow.
- Company attribution is by name and domain, so a company whose ledger name is
  generic ("Acme") or whose ATS page omits its name falls into `other job
  sites`; a ledger group named for a job site itself (LinkedIn's digests) never
  collects time.
- Another consumer of the moment must go through `Moment.snapshot` or
  `moment_facts`, or it will show a title or a URL where this decision allows
  none.
