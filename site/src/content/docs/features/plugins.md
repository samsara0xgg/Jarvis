---
title: Plugins
description: Jarvis reaches your Gmail, GitHub, Notion, calendar and lights through MCP plugins that are off until you turn them on, and she asks before changing anything.
---

Jarvis does not rebuild your apps. She connects to them through MCP, the open protocol that lets an assistant call another service's tools, and every plugin stays off until you turn it on.

## Which accounts can she reach?

Four plugins ship in the repository, and a fifth connection comes from configuration.

- **Gmail** searches, reads, drafts and sends mail. It runs Google's Workspace MCP server on your Mac with only Gmail switched on.
- **GitHub** triages repositories, issues, pull requests and CI through GitHub's hosted server.
- **Notion** reads and writes pages and databases through Notion's hosted server, with four bundled skills for specs, research, meetings and knowledge capture.
- **Philips Hue** switches and dims lights and recalls scenes. It talks only to the bridge on your home network.
- **Microsoft To Do and Outlook calendar** are not a folder in `plugins/`. They come through a Microsoft MCP server you add to her configuration yourself, then appear in the same plugin list, as can any other MCP server you add.

## How do you turn one on?

You do, and nothing starts by itself. No plugin is enabled out of the box. If you ask for something in an app that is not connected, she can open the plugin panel, but the sign-in begins only when you start it, and the daemon never opens a browser on its own. Tokens and keys go into private files only your account can read, never into the conversation. Those are plain files today, not encrypted.

<!-- shot: the plugin panel in the Dashboard, Gmail connected and Notion waiting for sign-in, sample data only -->

## What does she do before she acts?

She reads freely and asks before changing anything. Each tool is sorted by the hints its server provides. Read-only tools run at once. Anything that deletes, sends, merges or is not clearly harmless becomes a confirmation card that waits for your button. Only a write marked both non-destructive and self-contained skips the question.

You can tune this per plugin: always ask, ask only for writes, or never ask. Hue shows the idea: looking at your lights is pre-approved, but switching one asks first unless you relax it.

<!-- shot: a confirmation card for a Gmail reply under her last line in the conversation, sample message -->

## How does she find the right tool?

She searches for it. A server like GitHub lists dozens of tools, and sending them all with every request would cost money and slow every turn. So plugin tools stay off her menu until a turn needs them, and a single `tool_search` tool finds them by name, description and parameters. Found tools last for that turn, and a hit also brings the read-only tools of the same server, because a search is often useless without the read that follows. The calendar, To Do and Gmail reads you ask for most stay on the menu, so those questions skip the search.

## Where does your data go?

Each plugin talks to its own service. What she reads in a conversation, such as an email, goes to the model provider like everything else in it. Plugins are not signed and do not declare what they need, so turning one on means trusting it.

Design notes: [tools wait behind search](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0034-plugin-tools-wait-behind-tool-search.md), [approval per tool](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0033-mcp-tools-are-approved-per-codex-modes.md).
