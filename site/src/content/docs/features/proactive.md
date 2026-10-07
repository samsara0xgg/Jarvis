---
title: She speaks up, carefully
description: Cards at the notch, a quick check on how loud each one should have been, quiet levels, and one summary of whatever was held back.
---

Jarvis would rather stay quiet than interrupt you. When something needs you, a card comes to the notch, and every card lets you say whether it was the right amount of noise.

## When a card appears

Cards come mostly from your Claude Code and Codex sessions: a name pop when one finishes or stops, a card when one needs you (Allow and Deny, a plan, or a question), and a card when one finishes and asks you something.

Putting a card away, with Esc, the cross, a swipe or a click elsewhere, is not an answer. It never counts as Deny, and it stays on your list instead of nagging. While you are working inside Claude itself, she holds the pops back.

<!-- shot: a Claude Code needs-you card dropping from the notch with Allow / Deny, a small name pop above it; sample data -->

## Tell her how loud that was

Each card carries a small folded row asking whether its level was right. Open it to confirm the level or pick the one it should have had, from just logging it, through a glow, a card and a card with a sound, up to speaking. Confirmations you asked for, and pages you open yourself, do not ask.

Your answer stays on your machine, with a snapshot of the card: its kind, title, counts, tool name and the situation, such as the quiet level and the hour. It never keeps what an agent wrote, a command or a path. A card left alone for 30 minutes is logged as ignored. For now ratings are recorded, not learned from: the rules that set a card's level are fixed, and the log lets a better rule be tested against them.

<!-- shot: a card with the folded "Right level?" row opened, showing the confirm button and the five level choices; sample data -->

## Turn the volume down

Quiet is one setting held by the daemon, so the notch, the sounds and her voice agree, and it survives a restart. Each level includes the one before:

- **Quiet:** she never speaks unprompted and every cue sound is off. Cards still show.
- **No pop-ups:** also no cards. Claude Code's permission prompts go straight to its own dialog instead of waiting for a card you would not see.
- **Do not disturb:** also no visible reaction to anything outside; even the star marks stop updating.

Switch by voice with a fixed phrase, in Chinese or English, which runs without a model, or from her right-click menu. Reminders you set yourself ring at every level.

## Get one summary when you are back

Nothing held back is dropped. When you leave No pop-ups or Do not disturb, what waited arrives together: one thing is simply itself, and several become one card, "N things while you were away", needs-you first, then errors, then finished.

The same hold applies when she can see you are on a call or away. That needs the screen-activity reading switched on; without it she cannot tell, and holds nothing.

## Checks before she makes a sound

A cue sound needs quiet off, sound unmuted, no call or away, and private output: headphone-type, never built-in speakers, USB speakers or an unknown device. If she cannot tell, the card appears and nothing plays. A spoken line is stricter still: you must not be mid-conversation, and she must not have just spoken.

Design notes: [quiet levels](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0153-quiet-modes-are-one-daemon-owned-level-the-surfaces-read.md), [one feedback row for every card](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0160-every-proactive-card-takes-the-same-feedback-row-and-snapshot.md).
