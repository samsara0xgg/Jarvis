# ADR 0087 — Workbench previews in an isolated webview

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

The workbench prototype (`docs/design/agents-workbench.html`) opens
artifacts, web pages, PDFs and local HTML inside the Agents window: the
clicked card grows into the preview, the conversation folds into a narrow
column beside it, and the Long Exposure (ADR 0081) dims it. claude.ai
artifacts, the main case, refuse to load in frames. The window's own
browser session gets the daemon key added to every request to 127.0.0.1
(`electron/bridge.ts`), which reaches both the daemon and the agent host.

## Decision

Show pages in a `<webview>` bound to its own persistent partition
(`persist:agents-web`), with no preload, no Node integration and the sandbox
on, sending any new window to the real browser; read markdown and code
through the agent host and draw them in the page.

## Alternatives rejected

- **An iframe** — claude.ai sends frame-refusing headers, so artifacts would
  not load at all.
- **A WebContentsView** — it is a native layer above the page, so the card
  growing into it, its rounded glass, the sky dimming it and the column
  beside it cannot be drawn over or around it.
- **A webview in the window's own session** — every page shown would reach
  the daemon and the agent host with the key the main process adds.

## Consequences

Allen signs in to claude.ai once inside the preview's partition, and a
sign-in provider may refuse an embedded browser. Each open page costs a
renderer process.
