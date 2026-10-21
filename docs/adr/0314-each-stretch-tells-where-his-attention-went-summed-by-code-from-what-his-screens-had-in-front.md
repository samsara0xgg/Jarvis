# ADR 0314 — Each stretch tells where his attention went, summed by code from what his screens had in front

**Status:** Superseded-by-0330
**Date:** 2026-10-21
**Supersedes:** none

## Context

- Opening a stretch of the day timeline shows the items it holds (ADR 0307): one row per block of
  Mac work or phone use, each with the app names in it. Nothing says how many minutes went to what,
  or in what order. He called it "a series of boring text" (2026-10-20 23:46Z). He wants the
  stretch to open into its projects and its distractions, by attention: a thing that held most of a
  stretch is its main event, the rest is distraction or alongside, at a grain of about ten minutes
  (22:29Z, 23:11Z). On 2026-10-21 00:28Z he approved a sample day drawn that way and said to build
  it at that grain.
- TimeSink counts a span only while he uses the Mac: it stops counting while the Mac is idle,
  locked or asleep (ADR 0021). Each span has its app, its site, document or window title, and the
  seconds he typed in it. The apps he had in front on his iPhone and iPad come as stretches per
  device (ADR 0304), 6–8 hours late; past a device's newest record nothing is known yet.
- On 10-19 from 22:00 to 01:30 the Mac had him for 192 minutes. Jarvis took 35%, the film pages
  21%, WeChat 18% and building 一起看 17%. He confirmed these minutes. In the same evening the
  iPhone had WeChat in front for 43 minutes and browsers for 45, while he sat at the Mac.
- The writer was given these minutes as text for each quarter hour. In four replays on 2026-10-20
  it named 22:00–23:30 as building the site in one run and as watching the film with a friend in
  another, both marked high.
- The writer runs only at night and when he asks (ADR 0311). The page is drawn many times a day.

## Decision

Serve with each doing stretch where his attention went inside it. Code computes it from what his
Mac, phone and tablet had in front of him, and cuts it into rows where the lead changed for long
enough.

- **One place at a time.** His attention is on the Mac while TimeSink counts him there. Otherwise
  it is on the app in front on his phone or tablet, shared evenly while both have one. Otherwise no
  screen has it. An app in front on a device while he is at the Mac is alongside, never his
  attention. Typing goes with the time it was typed in.
- **Keys, not projects.** A Mac site is keyed by app and domain. Anything else on the Mac is keyed
  by app and document, else by window title. A device app is keyed by its bundle, the same key on
  both devices. Saying which project or person a key served is a model's judgement and is not
  made here. Once told, keys that serve one thread are summed as that thread. A key that serves
  whatever is around it (an untitled window, a login page) goes with the thread that held most
  within fifteen minutes of it. A key nobody told is its own thread.
- **Rows.** A stretch's rows are the ones of least total cost:
  - A row costs the attention minutes its lead leaves out, plus ten minutes for the row itself.
  - Rows are cut on the clock's five minutes, and found exactly.
  - Each row runs from the first to the last moment a screen had him inside it.
  - Each row tells its lead's minutes, the others' minutes and how often his attention left the
    lead. Distraction is told as minutes and switches, never judged.
- **Computed on read.** Nothing is stored, nothing is guessed, and the same records always give
  the same answer. A planned stretch has not happened and tells nothing.

## Alternatives rejected

- **Let the writer write the second level** — given the same minutes, it named the same 10-19
  stretch two ways in four replays. It also runs only at night and when he asks, so the second
  level would be stale all day.
- **Count phone time on top of Mac time** — 10-19 22:00–01:30 would count 192 + 88 = 280
  attention-minutes inside 210 minutes. A lit phone beside the Mac is not a second attention.
- **The lead of each quarter hour** — a quarter with no majority has no lead. On 10-19 at
  23:00–23:15 the film had 4 minutes and WeChat, the 放映厅 page and Claude about 2 each. A
  30-minute excursion that straddles three quarters reads as three changes. The least-cost cut
  gives one answer for any data, with one knob.
- **Totals per key, no rows** — "built 一起看, then Jarvis" and "switched between them all
  evening" have the same totals. The order is what a strip under the row draws.

## Consequences

- Every page read reads the stretch's TimeSink spans and device stretches from `shipped.db`.
  Finding the newest revision of each span groups every span key the Mac ever shipped, as the
  writer's brief already does once per run. Page latency on the Pi grows by that read.
- Until keys are mapped to threads, a key is as fine as its title. Untitled Claude windows, login
  pages and each Claude conversation are separate keys, so a row's lead can be a small key where
  its project would be larger.
- Phone and tablet time past a device's newest record is not there yet. A stretch's attention
  grows when the late batch arrives.
- Ten minutes is his "about ten minutes". Changing it re-cuts the rows of every past day.
