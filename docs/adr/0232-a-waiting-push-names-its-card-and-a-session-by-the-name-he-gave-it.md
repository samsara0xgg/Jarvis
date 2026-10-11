# ADR 0232 — A waiting push names its card, and a session by the name he gave it

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- A push that something waits for him (ADR 0210, ADR 0218) carried only a title, a line and a
  category. The phone could not tell which card a tap meant, so it could neither open that card
  nor answer it from a notification button. `GET /inherent/waiting` already serves every waiting
  card with its id.
- A Claude Code permission push read "tool · folder". For a session he runs from a Claude Desktop
  Project the folder is always `Projects`, so every prompt read "Bash · Projects".
- The session board titled a row by `claude agents --json`'s `name`. A Mac probe on 2026-10-11
  found that name is "Remote Control session (from a project)" for many project-thread sessions
  and an auto-made slug such as `projects-3e` for others. Claude Desktop keeps its own title per
  session in `~/Library/Application Support/Claude/claude-code-sessions/**/local_*.json`
  (`title`, keyed by `cliSessionId`). For a session spawned for a project thread it is the thread's
  title. 82 of 114 such sessions had a real title there; the rest kept the generic name.
- An app's login link (ADR 0202) was never pushed: asked for from the phone, it waited in a panel
  he could not see.
- A successful push left no trace, so whether his phone got one could not be read back.
- The owner asked on 2026-10-11 that these sessions show the names he gave them, and agreed that
  waiting cards, and logging in to an app, be done on the phone.

## Decision

Every waiting push carries its card's kind and id, and a Claude Code session is named by the
title Claude Desktop shows him.

- Beside `aps`, a waiting push carries `{"card": {"kind", "id"}}`: `confirmation` with its
  `confirmation_id`, `question` with its `clarification_id`, `claude` with the held request's id.
  The app opens that card from `GET /inherent/waiting` and answers on the card's own route.
- A session row's title is, in order: Claude Desktop's title for that CLI session id; the name
  from `claude agents`, unless it is the generic remote-control name or a slug of words ending in
  a short hex tail; the first prompt; the folder.
- A Claude Code permission push reads "session title · tool". Before the board has listed the
  session, the title is its folder. The tool's input is still never sent. A prompt used as a title
  drops the paths of files he attached, or is their names when it says nothing else.
- While an app's login link waits, a `plugin` push names the app, with the request's id as its
  card. It follows ADR 0218: only to the phone whose turn asked, never to a computer, and when no
  turn asked (a panel), only while he is away from the Mac. The app opens `auth_url` from the
  `plugin_request` of `GET /inherent/waiting`.
- Every alert sent logs one line with its outcome, device, category and card id, never its text
  or token.

## Alternatives rejected

- **Ask claude.ai for the project thread's title** — the Mac has no credentials for it, and
  Desktop already writes the title next to the session id.
- **Put the whole card in the push** — a push is limited to 4 KB, and the card can change or close
  before he taps; the id plus one read of `/inherent/waiting` is always current.
- **Keep `claude agents`' name first** — it is the placeholder for exactly the sessions he asked
  about.

## Consequences

- The phone app can open a tapped card and offer allow, deny or confirm on the notification. The
  app side of that is the app thread's work, built from this wire shape.
- A slug-like name he chose himself (words ending in two to four hex letters) gives way to the
  first prompt; Desktop's title still wins when there is one.
- Reading Desktop's files only reads `title` and `cliSessionId`, on the Mac that runs the
  sessions; a brain asks its terminal for the board as before (ADR 0170).
