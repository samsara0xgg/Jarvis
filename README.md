<p align="center">
  <img src="docs/assets/poster.png" alt="Jarvis, a glass ball with two glowing eyes, beside the words Jarvis, by Allen Shi" width="100%">
</p>

> **Demo video coming later today (October 1, 2026).** A short walkthrough of Jarvis running on my Mac will be posted right here.
>
> **In a rush?** You can try a demo on your own Mac in about a minute, with no keys or setup. [Click here to try it.](#try-the-demo)

<p align="center">
  <img src="https://img.shields.io/badge/macOS-000000?logo=apple&logoColor=white" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Electron-47848F?logo=electron&logoColor=white" alt="Electron">
  <img src="https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white" alt="React 19">
  <img src="https://img.shields.io/badge/TypeScript-3178C6?logo=typescript&logoColor=white" alt="TypeScript">
  <img src="https://img.shields.io/badge/MCP-1C1C1C?logo=modelcontextprotocol&logoColor=white" alt="MCP">
</p>

Jarvis sits next to the MacBook notch as a small glass ball with eyes. It answers when you talk to it, keeps a record of your day, and tells you when a Claude Code or Codex session needs you.

## What it does

- **What's left today, and what you did yesterday.** Answers come from your calendar, to-dos, git history and screen activity, and each item in the daily report points back to the record it came from.
- **Claude Code and Codex from the notch.** Each session is a star beside the notch. When one wants to run a command, a card drops down and you allow or deny it without switching windows.
- **Your limits.** The Usage page shows how much of your Claude and Codex plans you've used and when each limit resets.
- **English and Chinese.** It follows whichever one you speak.
- **Your accounts through MCP.** Gmail, Outlook, Microsoft To Do, GitHub and Notion connect as plugins, and it asks before it acts on any of them.

<p align="center">
  <img src="docs/assets/readme/agents-live.png" alt="Real Jarvis Agents view showing Claude Code and Codex sessions grouped by Needs You and Working, with the live session indicators at the top of the screen" width="100%">
</p>

<p align="center">
  <img src="docs/assets/readme/conversation-plugins-live.png" alt="A real English follow-up conversation with Jarvis, beside its connected Gmail, Linear, Microsoft and Notion plugins" width="100%">
</p>

<p align="center">
  <img src="docs/assets/readme/usage-projects-live.png" alt="Real Claude and Codex usage windows, reset times and OpenAI API spending, alongside a cropped seven-day Jarvis project activity chart" width="100%">
  <br><sub>Captured from the running app, with an English interface and a real conversation. Screenshots are cropped and arranged for this page; UI text and values are unchanged. Usage and activity are snapshots of one installation.</sub>
</p>

## Design notes

**Interrupting it.** The first version, [jarvis-legacy](https://github.com/samsara0xgg/jarvis-legacy), had an interrupt feature. Measured later, it had never fired once. Here you can talk over Jarvis at any point. With a reSpeaker XVF3800 mic array, its own voice is removed on the board before speech recognition hears it.

**Long answers.** A model's answer is written to be read, and read aloud word for word it drags. The full answer goes on screen, and Jarvis speaks a version of one to three sentences. Turns that call tools stay quiet until the answer is ready.

**Pops in the audio.** Faint clicks in replies looked like a streaming bug. They came from the speech provider's volume limiter, and went away with the volume left at its default.

**Keeping the code in shape.** The daemon is split into six layers, and only `runtime/` may connect them; `lint-imports` fails if anything else does. Each decision that moves a boundary gets a short record in [docs/adr](docs/adr), 54 so far.

## How it's built

A Python daemon owns the microphone, speaker, models, memory and tools. The companion is Electron with React and TypeScript, with a small AppKit module for the glass. They talk over localhost with a per-machine key. Wake word: microWakeWord. Speech recognition: SenseVoice with Silero VAD. Speech: MiniMax. Storage: SQLite. The full design is in [docs/spec.html](docs/spec.html).

## Run it

You need macOS, Python 3.12+, [uv](https://docs.astral.sh/uv/) and Node.js (tested with Node 24). On a MacBook it sits beside the notch; on a screen without one it lives in a small black pill at the top.

### Try the demo

To see the companion on its built-in demo data, with no daemon and no keys:

```bash
git clone https://github.com/samsara0xgg/Jarvis
cd Jarvis/desktop/resonance
npm ci
npm run companion -- --demo
```

What the demo shows: the companion beside the notch, its agent stars, and the Dashboard filled with sample data (calendar, to-dos, mail, the morning brief, agents). What it doesn't: it doesn't listen or speak, nothing in it is your own data, and the full Agents window (Startrail) doesn't open. Those need the full setup below. Press Ctrl+C in the terminal to quit.

### Run it for real

Start the daemon:

```bash
uv sync
uv run python -m jarvis serve
```

and the companion in a second terminal:

```bash
cd desktop/resonance
npm ci
npm run companion
```

On its first start the daemon downloads its speech models (about 240 MB) and runs text-only until they finish; voice works after that.

The first time it starts, the companion walks you through setup: your name, its language, an OpenAI key (required) and a MiniMax key for its voice. Each key is checked before it is saved to your login Keychain. You can also put keys in `~/.jarvis/env`, one `KEY=value` per line.

Anything that reads another app or account (agent sessions, usage, screen activity, plugins) is off until you turn it on in Settings.

Jarvis runs from a developer checkout. It is not a signed app yet, and a few defaults still assume the author's machine.
