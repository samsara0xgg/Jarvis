# ADR 0176 — The Dashboard Tells Jarvis What Is on Screen and She Can Turn Its Pages

**Status:** Proposed
**Date:** 2026-10-07
**Supersedes:** none

## Context

- Allen wants to run the Dashboard by talking to Jarvis (thread 「demo 构思」, 2026-10-07):
  "show me", "open the one from X", "the second one", with her seeing at least what he
  sees. Today only the mail page reports anything (ADR 0148: the open letter, every 20 s,
  kept 60 s); every other page is invisible to her, and no tool moves the Dashboard.
- "The second one" names a row by its place on screen. Only the page knows the order it
  draws; a tool result the model saw earlier does not.
- The state block (ADR 0148) is where per-turn facts ride without moving the cached
  prefix. Each producer is capped at 200 characters except the draft line (2000).
- Titles on screen are third-party text (mail subjects, company names) or Allen's own
  (memory items). ADR 0148 already sends the open letter's subject and sender to the reply
  model; bodies stay behind `gmail_get` and the other read tools.
- The Dashboard's view (page, tab, open item, search) is already one value in the shell,
  `DashboardView` (`AroundDashboard.tsx`), which every page reads; pages differ in what
  their rows are.
- Opening the panel uninvited takes over part of his screen; while he is in another app
  that is an interruption, which ADR 0131 keeps for things he asked for.
- `open_plugin` (L0, read-only) is the precedent for a tool that only moves UI. Read-only
  tools also run on the GPT-Live channel, which refuses writes (ADR-0016 D6).

## Decision

Behind one switch, `dashboard.view.enabled` (off: the route is 404, no line reaches the
model, the tool is not registered), the Dashboard shell reports its view and the model can
move it:

- **Report.** The shell posts `POST /inherent/view` on every view change and every 20 s
  while the panel is open, and `{page: null}` when it closes: the page, its tab, the open
  item `{kind, id, title}` and the rows in screen order, at most 10, each `{id, title}`
  flattened to one line and capped at 80 characters. The daemon keeps it in memory for
  60 s. It supersedes `POST /inherent/focus`: the mail page's open letter is the view's
  open item of kind `mail`, and `write_mail_draft` reads it from there.
- **Line.** One state-block line names the page, the open item and the numbered rows with
  their ids, capped at 1200 characters (the second exception to 200). Bodies, details and
  scores never ride it; the model reads them through the tool that owns them by id.
- **Move.** An L0 read-only tool, `show_on_dashboard(page, item_id?)`, pushes a `present`
  op to the companion, which opens the panel if needed, turns to the page and opens or
  briefly lights the item. It may name only a page that exists and an item id the current
  view or this turn's tool results carry; anything else opens the page alone. The prompt
  lets her move an open panel to follow the conversation, and open a closed one only when
  Allen asks to see something. Page `home` is the home screen; `close` folds the panel
  (`present` with no page).
- **Stay and go.** A panel she opened or turned does not fold when the pointer leaves: a
  window moving under a still pointer reads as a leave, and he is talking, not pointing. A
  click outside it, the notch, Esc or back closes it. When a spoken dismissal ends the
  conversation, the `controls` push carries reason `dismissed`, and a panel she opened
  folds with her; a panel Allen opened stays. The conversation timing out on
  silence closes nothing, since he may be reading what she opened.

## Alternatives rejected

- **Each page posts its own focus, as the mail page does** — twelve pages would each grow
  a poster and a staleness rule for one value the shell already holds; the mail page's
  20 s timer is the one copy to keep.
- **Send the rows' content (bodies, memory text, ledger detail)** — the Memory page alone
  holds hundreds of items; a line that grows with the screen costs every turn's tokens and
  time, while the read tools already return the same bytes by id when a turn needs them.
- **Let her infer the screen from `screen_look`** — a vision call per turn costs seconds
  and money and cannot see ids, so "open the second one" still could not name a row.
- **Open the panel whenever the answer is about a page** — while he is in another app it
  covers what he is doing; asked-for is the line ADR 0131 already draws.

## Consequences

- Up to ten titles (subjects, company names, memory items) reach the reply model on every
  turn while the panel is open, not just the open letter's subject.
- Another `live_context` producer runs at the start of every turn, and the state block can
  grow by 1200 characters.
- The mail page's focus route is retired: the companion and daemon must ship together,
  and the mail page now needs the view switch too, so the switch ships on.
- An id from a turn whose results have scrolled out of the view can no longer be opened
  by name; she must turn to the page and pick from its rows.
- Pages whose rows are not addressable today (the "现在" timeline, usage rings) report
  only their page, so "the second one" there resolves to nothing.
