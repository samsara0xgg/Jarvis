---
title: Mail
description: A small model sorts new Gmail into letters that need a reply and junk you can archive in one tap, and Jarvis can open a letter, sum it up and draft the reply, asking before she acts.
---

Jarvis sorts your unread mail so the letters that need you come first, and does nothing to your mailbox unless you tap or approve. It is optional and off until you switch it on, and it works through Gmail's MCP connection like her other plugins.

## How new mail gets sorted

She checks the unread Primary inbox every few minutes. For each new letter, a small model answers two questions: does the sender expect you to reply personally, and is this junk? A letter marked as needing a reply floats to the top and can trigger the pop-up. FYI mail stays quiet, and a letter the model is unsure about gets no mark at all. A letter that looks like it needs a reply is never also flagged as junk.

<!-- shot: Dashboard mail block, a few letters with "Reply" and "FYI" tags, sample sender names and subjects -->

It is deliberately cautious, because the two mistakes cost different amounts. A missing mark costs you nothing. A wrong "no reply needed" could hide something you owe, so the bar for either mark is high.

## What the sorting model sees

To keep letters private, the sorting model sees only the sender's display name and the subject line. It never gets the address, the body or a snippet, and the request goes to endpoints that don't retain data. Each letter is asked about once and the answer is remembered, so a letter that sits unread isn't resent every few minutes.

In a small test during design, a name and a subject were enough to tell a person from a notification, which is why the body stays on your machine.

## Clearing junk in one tap

When some letters look like junk, the mail block shows a single line, "N look like junk", with an Archive button. Archiving only removes them from the inbox; they stay in All Mail. It happens only when you tap, never on its own, and a toast offers Undo for a few seconds. Nothing is deleted automatically. On the Mail page you can also archive, trash or mark any single letter as read, and the trash is Gmail's own, which keeps letters for 30 days.

<!-- shot: short clip, tapping Archive on the junk line, then Undo in the toast, sample data -->

## Opening a letter and drafting the reply

The Mail page lists unread letters and opens one in place. Letters built as designed layouts, like newsletters and shop mailings, fall apart as plain text, so for those she shows a one-sentence summary and links to the original in Gmail. Plain letters show their text, with quoted earlier messages folded away. The summary comes from a different model than the sorter, because it has to read the letter.

"Draft a reply" asks her to write one. She knows which letter is open because the page tells her, so "reply to this" means this one. The draft appears as text you can edit, and she revises from your edits. Send doesn't send: it raises the same confirmation card used everywhere else, with the letter as it will go, and only your confirmation sends it.

<!-- shot: Mail page, a letter open with a one-line summary and an editable reply draft below, sample data -->

## Asking before acting

Reading mail needs no permission. Anything that changes the outside world, such as sending, goes through confirmation. The one exception is the taps you make yourself on the Mail page, which are your own actions, and each of those can be undone.

Design notes: [Gmail through MCP](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0055-gmail-comes-through-googles-workspace-mcp-server.md), [the Mail page and drafts](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0148-the-dashboard-mail-page-tells-jarvis-what-is-open-and-shows-his-draft.md)
