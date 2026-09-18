<h1 align="center">Jarvis</h1>

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-Frontend-3178C6?logo=typescript&logoColor=white)
![React](https://img.shields.io/badge/React-UI-61DAFB?logo=react&logoColor=white)
![Electron](https://img.shields.io/badge/Electron-Desktop-47848F?logo=electron&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Memory-003B57?logo=sqlite&logoColor=white)

**A personal AI assistant that keeps track of your work and helps you pick up where you left off.**

## What Jarvis Helps You Do

Jarvis is being built around a continuous daily loop: review yesterday’s progress and plan the morning, follow project work throughout the day, and consolidate conversations, discoveries, and unfinished tasks into context for what comes next.

Knowledge consolidation happens in the background as information accumulates, preparing the context for your next briefing. The goal is to make each day build on the last, with less time spent reconstructing what happened and deciding where to resume.

## How It Works

See the [architecture specification](docs/spec.html), [interactive architecture](docs/archify/jarvis.architecture.html), and [task execution diagram](docs/archify/task-run.sequence.html). Download/open the HTML diagrams locally to explore them.

<!-- A revised end-to-end overview diagram will be added here after its scope is settled. -->

## Core Capabilities

### Observation & Agent Awareness

Jarvis gathers context from your work and interactions. Configurable Git observers record repository state and new commits, while on-demand tools can inspect the screen, retrieve notes, and look up information.

Agent awareness extends this context to work delegated to other AI systems. Integrations with tools such as Codex, Claude Code, and Hermes Agent are being developed to track what each agent is working on, its progress, and whether it needs attention.

These observations build a background picture of ongoing work. Routine changes can remain quiet while staying available when you ask what is happening or where a task stands.

### Persistent Memory & Context

Jarvis preserves conversation records across sessions and retrieves relevant history when needed. Each new session receives a bounded context brief assembled from stored information, helping you resume without reconstructing the background.

A persistent background session is being developed to maintain continuity beyond individual conversations. Recent exchanges remain available in detail, while older material can be compacted into summaries for efficient context use. Compaction preserves the original records; when a summary is insufficient, Jarvis can retrieve the underlying conversation from the raw history.

Over time, this accumulated history provides a richer basis for understanding your preferences, projects, and working habits. The goal is increasingly relevant assistance, with remembered information that can be traced back and corrected.

### Tasks & Execution

Jarvis maintains task and execution records, routes requests to tools, and applies permission checks around actions. Persisted events make it possible to inspect what was requested, what ran, and what result was observed.

Backend queries run independently of the conversation. Request deduplication prevents repeated submissions, and superseded results are withheld from the current spoken interaction.

Further work connects these execution records with external agent activity, task updates, and reminders so that ongoing work can be followed through to a recorded outcome.

### Resonance Desktop

Resonance brings conversation controls, transcripts, and runtime information into a desktop interface.

Its dashboard already includes usage monitoring for configured providers, with coverage and reliability still being expanded. The broader direction is a shared view of active agents, project progress, API usage, subscription quotas, and reminders.

Some dashboard sections remain previews. Agent monitoring and reminder workflows are still in development.

## Interfaces & Providers

Jarvis currently runs as a resident macOS application, with a Python backend and the Resonance desktop interface communicating over local HTTP and WebSockets.

### Configurable Backend Models

Jarvis supports configurable backend LLMs through OpenAI-compatible APIs and the Anthropic API. You can choose a model, endpoint, and credentials to suit different tasks or providers.

OpenAI-compatible endpoints allow many additional model providers to be connected through the same integration style. Tool calling, streaming, vision, and other capabilities still depend on the selected provider and model.

### Voice

Jarvis currently offers two voice paths:

- **GPT Live:** native real-time conversation with asynchronous delegation to the Jarvis backend. The current delegated tool set is read-only.
- **Native voice pipeline:** Jarvis’s own speech-recognition → backend LLM → speech-synthesis pipeline, with separately managed stages.

Both provide an interface to Jarvis’s backend capabilities. Their controls and session behavior differ; a common, replaceable voice-provider interface remains a development direction.

## In Development

### Completing the Daily Loop

- **Morning briefings:** bring together yesterday’s recorded progress, unfinished work, and today’s tasks.
- **Work activity tracking:** combine Git observations with lightweight application and idle-activity signals.
- **Background knowledge consolidation:** turn conversations and discoveries into useful summaries with source references.
- **Agent coordination:** expand progress and state tracking across external AI agents.
- **Plan updates:** complete and reschedule tasks through conversation.
- **Proactive assistance:** add configurable triggers, quiet periods, daily deduplication, and deferred reminders.
- **Daily reliability:** improve audio recovery and verify the complete workflow across repeated sleep/wake cycles.

### Planned Extensions

- **Raspberry Pi room endpoint:** collect room signals independently of the Mac and support a dedicated voice interface.
- **ESP32 sensors:** add presence, motion, and other environmental observations.
- **Camera-assisted perception:** supplement presence and activity estimates with visual signals.
- **Speaker identification:** use voice identity alongside other observations.
- **Multi-sensor context:** combine signals over time to better understand the surrounding environment.
- **Selective screen observation:** capture meaningful changes that help maintain work context.
- **Additional voice providers:** expand real-time voice options while retaining Jarvis’s memory and task state.

## Getting Started

This is a developer checkout, not a notarized, one-click macOS application. Configuration still reflects the author's setup and should be reviewed before use.

Prerequisites: macOS, Python 3.12+, uv, Node.js/npm, and Xcode Command Line Tools for the desktop's native material module. Voice requires working audio devices, microphone permission, and the model assets selected in the configuration.

```bash
git clone https://github.com/samsara0xgg/Jarvis.git
cd Jarvis
uv sync --extra dev
```

Review [config/jarvis.yaml](config/jarvis.yaml):

1. Select an available backend model in `llm.presets` and provide the environment variable named by its `api_key_env`. The current default uses `DEEPSEEK_API_KEY`.
2. GPT-Live uses `OPENAI_API_KEY` and requires access to the configured Live model.
3. Review the local voice model/device settings and any optional provider keys.
4. Set `observer.repos` to the repositories you want to observe.
5. Review [file targets](config/file_targets.yaml) for your own machine.

Keep credentials outside the repository. The runtime supports a local `~/.jarvis/env` file for resident service credentials.

Start the backend in one terminal:

```bash
uv run python -m jarvis serve
```

Build and start the desktop in another:

```bash
cd desktop/resonance
npm ci
npm start
```

See [Resonance setup](desktop/resonance/HANDOFF.md) for detailed build requirements and integration boundaries. After configuring and checking the application manually, use `uv run python -m jarvis daemon install` to enable login startup, `uv run python -m jarvis daemon status` to inspect it, and `uv run python -m jarvis daemon uninstall` to remove login startup.

## Development & Verification

Run the local delegation check without cloud calls:

```bash
uv run python scripts/smoke_gpt_live_delegation.py
```

Run automated tests without live LLM or Codex scenarios:

```bash
uv run pytest tests -m "not live_llm and not live_codex"
```

Some scenarios exercise paid APIs or external runtimes and are kept separate. Automated checks do not substitute for real audio, sleep/wake, and multi-day usage verification. See the [development guide](docs/git-guide.md) and [recorded recovery experiment](docs/live-burn-2026-09-05-crash-recovery.md).

## Project History

Jarvis began as a custom ASR → LLM → TTS voice assistant and evolved into a resident runtime with persistent context, tool execution, and multiple interaction paths.

The earlier implementation and its historical experiments remain available in [jarvis-legacy](https://github.com/samsara0xgg/jarvis-legacy). Its benchmarks describe the versions where they were measured, rather than the current GPT Live path.
