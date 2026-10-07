---
title: Memory
description: Jarvis keeps each past day as a short summary and rewrites what she knows about you every night as an undoable version, with every note traceable and correctable.
---

Jarvis remembers in two layers: a short summary of every past day, and a small note of lasting facts about you that she revises overnight. Both are derived from the full transcript, and you can inspect and fix both.

## What she keeps from each day

Once a day she writes a summary of every past day that has none yet. Each has three parts: what you talked about, what you decided or stated, and what was left unfinished. Every line points back to the record it rests on. A day with very little said is kept word for word, since a summary of a few lines is no shorter than the lines.

In conversation she sees the last three day summaries, then every word from the day after the newest summary onward. Anything older she looks up when needed. The prompt stays small, and the exact wording of any day is still in the transcript, which is never rewritten.

<!-- shot: Memory page, Days tab, one day card expanded showing the three sections, sample data -->

## What she knows about you, as versions

The note of lasting facts has six sections: about you, preferences, people, what you're working on, rules, and commitments. It is capped (4,000 characters by default) because it rides along in every conversation, so one wrong line costs every later turn.

She never edits it in place. Every change, whether she remembered something mid-conversation or the night pass added it, is a new version, and old versions are kept. Overnight a model reads each unprocessed day and proposes typed changes: add a note, rewrite one, or mark one stale. Each proposal must cite specific lines from that day's transcript. If any check fails, the whole batch is thrown away and asked for once more. A to-do whose date has passed is marked stale by plain date matching, with no model involved.

So nothing she writes about you is unaccountable: a note without a citation never lands, and a bad night can be rolled back.

## Checking and correcting a note

The Memory page lists every note by section, with last night's additions on top. Open a note and it shows the words you or she said that it came from, so you can judge it against the evidence. You can confirm it, edit it, move it to another section, or delete it. A search covers what's been said, and the change log lists every version with an undo.

<!-- shot: Memory page, one note opened, "where it came from" quote visible with edit and delete actions, sample data -->

Each of those actions is itself a new version, so each can be undone. If a later change has touched the same note, the undo is refused and names it, rather than guessing what you meant.

## Why your edits are pinned

A note you edit or move is pinned. The night pass drops its own rewrite of it, and if the note looks out of date she only leaves you a reminder. Confirming a note as right does not pin it, so it can still be updated as things change. The reasoning: an edit that the next night quietly overwrites is worse than no edit.

<!-- shot: short clip, editing a note, saving, and the "Yours" tag appearing, sample data -->

Design notes: [core memory](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0146-core-memory-is-a-versioned-note-the-night-consolidates.md), [pinned edits](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0154-memory-page-edits-are-versions-and-what-the-user-touches-is-pinned.md)
