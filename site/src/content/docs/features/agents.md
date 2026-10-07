---
title: Claude Code and Codex from the notch
description: Your coding agents appear as stars beside the notch, and when one needs a decision a card drops down so you can answer without switching windows.
---

Jarvis watches your Claude Code and Codex sessions so you don't have to keep checking terminals. Each one shows up beside the notch, and when one needs you, the question comes to you.

<!-- shot: the notch with a small row of stars to its right (one "needs you", two "working", each with a count), sample sessions only -->

## See every session at a glance

The row beside the notch groups sessions by what they need from you: your turn, working, finished and parked, each with a count. A working star turns slowly and one that needs you pulses.

Rest the pointer on the row and the notch grows into a list with one line per session: its name and what it is doing now. From there you can read its last answers, park it until later, or archive it. She holds back cards for a session you are already looking at in its terminal.

<!-- shot: the notch panel open, sessions listed under "Your turn" and "Working", one line each, sample project names -->

## Answer permission requests where you are

When Claude Code wants to run a command or edit a file, a card drops from the notch showing the command and the folder it runs in, and you allow or deny it there. The same card handles Claude's questions and plans: pick an option, approve the plan, or say what should change.

<!-- shot: a card hanging from the notch with a shell command, Deny and Allow buttons, sample command `npm run build` -->

She does this by holding Claude Code's own permission hook until you answer, and never by replacing its prompt. The terminal dialog stays open beside the card and whichever you answer first wins. If nobody is looking, because the companion is closed or you have switched on a quiet mode, she lets go and Claude Code asks the way it always did, so an agent is never stuck waiting on a card nobody can see.

Codex is shown, not answered. A Codex approval appears as a "needs you" card that takes you to Codex, because Jarvis only listens to Codex and never decides for it.

## Catch a "finished" that is really a question

An agent that ends its turn with "which of the two do you want?" looks like it simply finished. An opt-in setting lets a small hosted model read the end of the final message and, only when confident, show it as "needs you" instead of "done". It can promote a finish, never hide one, and is off by default because that text leaves your Mac.

## Startrail, the Agents window

Startrail is a fuller window for running Claude Code and Codex sessions side by side. It orders its list by who has waited longest, gives new sessions their own git worktrees, and can land a session's work into the repository. While it is in front the notch stays quiet, so you never get the same alert twice; behind other windows, its sessions join the stars beside the notch with the same cards.

It is still in progress. Startrail opens only from a source build on a Mac with your own Claude Code and Codex sign-ins. The demo and the installed app don't open it, since shipping Claude Code inside an app needs Anthropic's terms settled first. What you can try today is the Dashboard's Agents page, where the demo holds sample sessions to approve or deny.

<!-- shot: the Dashboard Agents page with "Needs you" and "Working" groups, sample sessions, Approve and Deny buttons visible -->

Reading Claude Code's session list is off until you turn it on, and the two small hook scripts that feed the cards are copied in by hand for now, because there is no installer yet.

Design notes: [Jarvis holds Claude Code permission prompts](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0049-jarvis-holds-claude-code-permission-prompts-for-the-notice-card.md), [Jarvis's notch says what Startrail would notify](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0104-jarviss-notch-says-what-startrail-would-notify.md)
