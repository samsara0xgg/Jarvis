---
title: "How it's built"
description: "A Python daemon owns the microphone, speaker, models, memory and tools; an Electron companion owns the screen. Six layers keep the daemon's code in order, and one record per decision explains why."
---

Jarvis is two programs on one Mac that talk only to each other, and a daemon whose code is split into layers it is not allowed to blur. This page is the map; [Design notes](design-notes/) tells four of the decisions behind it as stories.

## Two processes

**The daemon** is Python 3.12. It owns the microphone and speaker, the local speech models, memory, the tools, and every request that goes to a language model. Nothing else on the machine touches those.

**The companion** is Electron with React 19 and TypeScript, plus a small AppKit module (Objective-C++) that draws the glass. It owns what you see: the ball beside the notch, the Dashboard, setup and settings. It holds no models and no memory. A small Node host starts beside it when you open the Agents window, to keep your Claude Code and Codex sessions alive across restarts.

They talk over localhost with a per-machine key. The daemon answers only requests addressed to `127.0.0.1` or `localhost` (so a web page that rebinds its own domain to your machine gets nowhere), and every route except the liveness probe needs the key from the owner's runtime directory.

## The voice path

The wake word is microWakeWord ("Hey Jarvis"). Speech recognition is SenseVoice with Silero for voice activity detection, and local Whisper can take over for longer turns. Her voice is MiniMax streamed over WebSocket. Wake word, VAD and ASR run on the Mac; the first run downloads about 240 MB of models and the daemon runs text-only until they arrive. Storage is SQLite throughout.

## Six layers

The daemon's code is six packages, numbered by what each is allowed to know.

| Layer | Owns | Example module |
|---|---|---|
| L1 constitution | The product's identity, principles and non-goals, as frozen constants. Imports nothing. | `jarvis/constitution/__init__.py` |
| L2 state | The append-only event log, projections folded from it, `memory.db` and the other local stores | `jarvis/state/event_log.py` |
| L3 decision | Tier 0 routing, policy and gates, building each turn's model request, the daily report and work-state analysis | `jarvis/decision/gates.py` |
| L4 execution | Tools, workers, the plugin and MCP client, the browser guard | `jarvis/execution/mcp_tools.py` |
| L5 surface | Voice, the HTTP and WebSocket interface, Claude Code and Codex sessions, observers for screen and usage | `jarvis/surface/voice_pipeline.py` |
| L6 deployment | The runtime directory, launchd, sleep and wake, model downloads, export and erase | `jarvis/deployment/launchd.py` |

Around them sit three packages that are not layers: `jarvis/runtime/` (wiring, `daemon.py`), `jarvis/cli/` (the entry point) and `jarvis/shared/` (cross-layer reference data such as `pricing.py`, importable from anywhere).

### Only `runtime/` wires across layers

Imports run one way. `cli` may import `runtime`; `runtime` may import the four middle layers; those four (decision, execution, surface, deployment) may import `state`; `state` may import `constitution` and `shared`; and those two import nothing. The four middle layers are siblings that cannot import each other, so the voice code cannot reach into the tools and the decision code cannot reach into the voice.

This is a rule the build enforces. `lint-imports` reads the contract in `.importlinter` and fails if any package breaks it; it runs in the repository's setup script and is a gate before every commit. When two layers have to cooperate, `runtime/` hands one a function from the other. For example the decision-snapshot cache lives in `jarvis/runtime/decision_state.py` and reaches the decision layer as a reader function it is given, so the decision layer imports nothing from it.

## The state spine

Everything Jarvis knows flows one way: **event log, then projections, then memory**.

- The **event log** is a SQLite table that only ever grows. Triggers reject every `UPDATE` and `DELETE`; a correction is a new event. Each event records who reported what and when, event types are registered in one file, and large things like screenshots and recordings are stored as files with only a reference in the log.
- **Projections** are folds of that log: pending confirmations, open actions, the recent turns. Models, tools and screens cannot write them. Each turn reads the fold up to the latest event, and since ADR 0164 it folds only the events added since the last read, with the same result as folding from the start.
- **Memory** is `memory.db`: the verbatim conversation, the per-day summaries, and the versioned note about you that the night run rewrites. Every layer above it is derived and can be rebuilt.

## How decisions are recorded

Every decision that moves a boundary or a contract gets an ADR in [`docs/adr`](https://github.com/samsara0xgg/Jarvis/tree/main/docs/adr): 177 records so far, one decision per file, one page each. The standard (`docs/adr/README.md`) makes an **Alternatives rejected** section mandatory, with losing reasons someone could refute with a number, because that is the one thing the code and the spec never say. `scripts/check_adrs.py` checks the format, the status vocabulary, the 200-line cap and dangling references. To change an accepted decision you write a new ADR and mark the old one superseded; you do not edit it.

## The full specification

What a public build has to satisfy is written down in [`docs/spec.html`](https://github.com/samsara0xgg/Jarvis/blob/main/docs/spec.html): processes, the state spine, how a turn is acted on, what leaves the Mac, threats and their guards, keys, lifecycle. It is written in Chinese; the ADRs are in English.
