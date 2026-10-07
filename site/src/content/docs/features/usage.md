---
title: Usage, limits and spend
description: One page shows how much of your Claude and Codex plans you have used, when each limit resets, and where your tokens went by day and session.
---

The Usage page answers two questions in one place: how much of my Claude and Codex plans is gone, and when does it come back. Under that it shows where your tokens went.

<!-- shot: the Usage page top to bottom: balances row, Claude and Codex rings with reset countdowns, OpenAI spend ring; sample percentages only -->

## See how much of each plan is left

Each subscription gets a ring per limit window. For Claude that is the five-hour window and the weekly one, plus a separate weekly ring when the plan has a model-specific cap. Codex shows its own window. Under every ring is a countdown to the reset. A ring warms up as it fills and turns urgent near the top, so a limit that is about to stop you mid-task is hard to miss. A smaller version sits on the Dashboard home page.

Jarvis gets these numbers from the Claude Code and Codex sign-ins already on your Mac. It polls about every five minutes and stores only percentages, amounts, times and plan names, never a credential. The plan numbers are opt-in for exactly that reason: it reads another app's login.

The numbers come from endpoints the providers don't document. When one changes, the row says what went wrong instead of showing a wrong number. Whether this page ships in the public build is still undecided.

## Spend a Codex reset on purpose

Codex sometimes grants limit resets, and Claude shows how many you have left and until when. On the Codex side the page can spend one for you, but only after a confirmation that takes a moment to arm, so a double click can't land on it. Each confirmation can spend exactly one reset, even if you retry.

<!-- shot: the "Use this reset?" confirmation under the Codex ring, text "Uses 1 of your 2", sample data -->

## See where the tokens went

The Token section reads the local logs of Claude Code and Codex and shows the last 30 days: a bar per day, a share per agent, and a list of sessions ordered by cost. Pick Today, 7 days or 30 days, and open a session to see which models it used.

Everything is priced at API rates, so read it as a yardstick for effort. On a subscription it is not a bill. The calculation happens on your Mac from files already there.

<!-- shot: the Token section with a 7-day stacked bar chart, per-agent shares and three sessions, one expanded to show models; sample session titles -->

With an admin key, OpenAI's API spend shows today as a ring cut by model, with the month so far. OpenAI reports no balance, so you type the one from its billing page and Jarvis subtracts what you spend after it, shown as approximate.

## Get a nudge when a day's spend runs high

The first time a day's recorded model spend crosses a limit, one card comes down from the notch and says so. The default is a modest dollar a day, and you can change it.

It never stops, slows or switches a model. Going quiet in the middle of a conversation seemed worse than overspending by a few cents. The card comes silently, at most once a day, and follows your quiet modes like her other cards.

The total counts only priced model calls the log records. Voice synthesis, the small helper model and web search aren't in it, so the real day is a little higher than the card says.

<!-- shot: a silent card under the notch reading "Today's spend is over the limit" with a "spent today, limit" line; sample numbers -->

Design notes: [One notch card when a day's recorded spend first crosses its limit](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0173-one-notch-card-when-a-days-recorded-spend-first-crosses-its-limit.md), [The Usage page spends a Codex reset after two clicks](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0048-the-usage-page-spends-a-codex-reset-after-two-clicks.md)
