# ADR 0059 — A driven browser leaves payment and sign-in to Allen

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** none

## Context

- Allen, 2026-09-25: Jarvis should book hotels and flights for him now, but
  the capability stays out of the public version. It searches, compares and
  fills in; he pays himself, in the same window.
- Meta's Muse, studied the same day, runs its browser in a per-user cloud VM,
  keeps logins in a store the model cannot read, and shows every checkout
  through an approval service outside the model. Jarvis runs on Allen's Mac
  and operates no per-user cloud machines.
- Playwright MCP (`@playwright/mcp` 0.0.82) drives a real Chrome and lists 25
  tools; every tool that is not read-only is marked destructive. Under ADR
  0033's `auto` mode each click is a confirmation question, and only the
  first one in a turn is asked; the rest are refused.
- Its page snapshot prints every textbox value, passwords and card numbers
  included (`textbox "Password" [ref=e6]: hunter2secret`, observed). A field
  with its own placeholder carries the value on a child line instead.
  `browser_find` answers "found" or "no matches" for any pattern, which spells
  a value out one character at a time. In its default snapshot mode it also
  writes every snapshot to a file.
- A click target is a snapshot ref or any selector. Enter in an ordinary field
  submits the form around it, and a click on an icon inside a button presses
  the button. `browser_evaluate` and `browser_run_code_unsafe` run arbitrary
  script in the signed-in page.
- Spec §13.4: the runtime enforces a high-risk rule, never the prompt.

## Decision

Run every tool of a server that lists `browser_snapshot` behind an L4 guard.
Before each call that changes the page, and before a search of it, the guard
takes a fresh snapshot of its own. It refuses the call when:

- the target is not a ref from that snapshot;
- the target, or a control it sits inside, commits money;
- the call types into a sign-in, verification or card field;
- the page shows a card field;
- a key other than a movement key would be pressed, or typing would submit;
- a dialog other than an alert would be accepted.

A result that shows a filled sign-in, verification or card field is withheld
whole, and a search runs only on a page that holds none.

The browser itself is a plugin in Allen's runtime directory, so no public
build carries it. The plugin sets:

- Chrome with its own profile;
- snapshot mode `none`;
- Codex's `enabled_tools`, which leaves out script, upload and network tools;
- approval mode `approve`.

## Alternatives rejected

- **ADR 0033 confirmation per browser action** — a booking is dozens of
  page-changing calls. Only the first in a turn is asked and the rest are
  refused, so a booking never finishes.
- **A prompt rule to stop before paying** — §13.4. A model that misreads one
  button pays.
- **Muse's per-user cloud VM with a credential store** — Jarvis would operate
  and pay for a machine per user. Its requests would also leave from datacenter
  addresses, which is how Amazon blocked Muse wholesale on 2026-09-21. Allen's
  Mac browses from his home address.
- **Driving Allen's everyday Chrome through an extension or its profile** —
  Muse's own extension path has no approval step and returns password values
  (`commands.js:1649`, `browser-kit.js:116` in Muse.app 2.2). A separate
  profile holds only the sessions Allen signs into in the Jarvis window.
- **Jarvis's own Playwright tools** — the ref snapshot, frames, dialogs and
  tabs would be rebuilt in Python and then kept current. Taking Playwright MCP
  as a connector leaves the guard as the only Jarvis-side code.

## Consequences

Money controls are recognised from word lists, not by meaning:

- A control that charges under another name, such as a "Continue" that pays
  with a saved card, gets clicked. Allen watching the window is the backstop.
- "Book now" and "Reserve" are not on the list: on hotel sites they open the
  details form.
- A page with any card field refuses every page-changing call, harmless ones
  included.
- A password field with no accessible name keeps its value in the snapshot.
- A page where Allen has typed or autofilled a password stays unreadable to
  Jarvis until the field is gone or empty.
- Search boxes need their button clicked; Enter is never pressed.

Other costs:

- Every page-changing call costs one extra snapshot round trip.
- A booking takes 16 to 18 tool requests, over the 5 a turn used to allow;
  ADR 0060 moves that bound into config.
- Other sends through a web page, such as a comment or a web message, run
  unasked. The `approve` mode ADR 0033 already allows is what lets them
  through.
- Pages larger than the 8 KiB result budget are cut to their head and tail.
  The model reads the rest through `browser_find` or a snapshot's `target`
  and `depth`.
