# ADR 0247 — The Dashboard takes an item by number, one tool dismisses cards, and four tools wait behind tool_search

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** ADR 0075, and the rejection of reusing `withdraw_card` in ADR 0206's alternatives. Everything else in ADR 0206 and ADR 0176 stands.

## Context

- Replay exam, logged 2026-10-06: "Open the second one." with the Dashboard on its home screen
  made the model call `show_on_dashboard` with a guessed `item_id` 29 times in one turn, each
  answered `item: null`. Three exam cases fail on it (`dash-guess-first`, `dash-guess-retry-1`,
  `dash-guess-retry-11`). After "the Dashboard is shut" the model reopened or closed it instead
  of answering (`dash-shut-repeat`).
- ADR 0176: the shell reports its page and up to ten rows in screen order on every change and
  every 20 s, so the program knows the order only while the Dashboard is on that page. The
  state block already numbers those rows.
- Two tools took a card down: `withdraw_card` for a confirmation (ADR 0075) and `close_question`
  for a question (ADR 0206). With no card waiting each failed, and the model repeated the call
  (ADR 0240, `mem-churn-withdraw-card`). `cal-today-ask-user` and `cal-schedule-ask-user` show it
  raising an `ask_user` card and closing it in the next call.
- Four tools no turn needs by default are 5,456 characters of description and schema on the menu
  the exam rebuilds offline: `save_briefing`, `save_knowledge`, `search_knowledge`,
  `record_application`.
- No code reaches those four through the model's menu. The daily report writes its briefing
  through `daily_store` and the Jobs ledger through `JobMail.add_application`. The plugin panel's
  four tools (`list_plugins`, `open_plugin`, `disable_plugin`, `remove_plugin`, 2,168 characters)
  are another case: `prompts/jarvis_v1.md` sends the model to them by name when he asks to connect
  an app, and that is a direct flow, so they stay on the menu. `ToolRegistry.dispatch` and the
  decision loop's lookup ignore `deferred`; only the menu honours it.
- `tool_search` (ADR 0034) indexed MCP tools only.

## Decision

`show_on_dashboard` takes `item=N`, the item's number among the rows the Dashboard shows on that
page counted from 1, `dismiss_card` replaces `withdraw_card` and `close_question` and does
nothing without an error when no card is waiting, and the four tools above are deferred, with
`tool_search` indexing built-in deferred tools as it does MCP ones, except a built-in a menu tool
took over (ADR 0242), which stays found only by naming it.

Limits:

- A number is read only while the Dashboard is on the page named. From another page, or shut and
  asked to open, the page opens alone and the result says the number was not known. Past the
  rows it is an error naming how many there are. `item_id` stays for an exact id a tool
  returned, and both together is an error. A shut Dashboard he did not ask to open stays shut.
- `dismiss_card` takes down the waiting confirmation card (its action never runs) and the
  waiting question card; with both up, both go. It cannot accept anything.
- Deferring changes who may call a tool and what asks first for none of them.

## Alternatives rejected

- **Resolve the number in the shell after it turns the page.** It would work from any page,
  but it adds a field to the `present` wire op and ranking code to `AroundDashboard.tsx` for the
  one case, a number said from another page, where the replay shows the model already holds ids
  from a tool result; the guess loop came from an id, and a number read from the rows reported
  stops it.
- **Keep both card tools and make `withdraw_card` idempotent.** The exam's two `ask_user` cases
  show the model reaching for the close tool unasked; two no-argument schemas on the menu (647
  characters) for one act leaves a name to pick wrongly, and ADR 0206's fear of one tool with two
  meanings did not hold: both acts take down a card and neither takes an argument.
- **Defer the plugin panel's four too.** Connecting an app by voice is two or three calls in a
  row (`list_plugins`, then `open_plugin`), and the prompt names both; a deferred pair costs a
  `tool_search` round before the first of them, to save 2,168 characters on a menu of 36,224.
- **Offer the four to background callers only.** No background caller dispatches them through
  the model's menu; a new principal would guard a path nothing uses.
- **Delete the unused tools.** Each has a live path: the Jobs tracker (ADR 0213), the plugin
  panel (ADR 0202), the daily report and knowledge store (ADR 0036).

## Consequences

- Using a deferred tool costs a `tool_search` round first, and only works if the model thinks to
  search: "I applied to Acme" needs a line in `prompts/jarvis_v1.md` that sends it to
  `record_application`.
- A search that shares a word with a deferred built-in loads it as well ("search" brings
  `search_knowledge`), and a loaded tool's schema rides the rest of the turn.
- The runtime publishes `tool_search` once every built-in is registered
  (`PluginConnections.refresh_search`), so the MCP group and `tool_search` come after the built-ins
  on the menu.
- Saying no to a confirmation card while a question card is up takes the question down too.
- From another page, "open the second one" opens the page and asks which one; only the next turn,
  whose state block lists the page's rows, can name the number.
