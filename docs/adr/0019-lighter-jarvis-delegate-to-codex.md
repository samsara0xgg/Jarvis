# ADR 0019 — Lighter Jarvis: delegate to Codex, drop the audit chain

**Status:** Accepted
**Date:** 2026-09-19
**Supersedes:** none

## Context

- Four months of the live event log (`~/.jarvis/mac_events.db`, 4657 rows)
  hold `task.created` 1, `task.verified` 1, `run.started` 3. The Codex
  verification chain that produces them — L2 `TaskLedger`,
  `ClaimEvidenceProjection`, `EntityRegistry`; L3 `result_interpreter`,
  `reviewer`, most of `gates.py`; L4 `action_runner`, `codex_action`,
  `diff_capture`, `verify_command_detect` — is roughly 7k lines and ran once.
- The action pipeline (`action.proposed` … `action.result_observed`,
  `authorization_snapshot`, `authorized_dispatch_outbox`, `lifecycle_terminal`)
  is not part of that chain: all 86 live `action.proposed` rows are
  `get_current_time` and `web_search`, and the confirmation gate lives there.
- `spawn_worker` takes only `{task_id}`, runs a whole Codex turn synchronously
  on the dispatch thread, mints no id the model can hold, and has
  `allowed_callers=frozenset()` since 2026-09-12. No LLM can call it.
- Codex ships its own sub-agent model (`multi_agent_v1`, default on):
  `spawn_agent` returns an id immediately, `wait_agent` / `send_input` /
  `close_agent` act on it, status is one enum, and the only thing that crosses
  back to the parent is the child's final message. Every worker Jarvis runs
  is a Codex thread, so this model is available over `codex app-server`
  without porting any of it.
- Codex's `hooks` feature (stable, default on) fires `SessionStart`,
  `UserPromptSubmit`, `PermissionRequest`, `Stop` and eight more events from
  the engine, including the copy the ChatGPT desktop app runs. The app's
  transport is `stdio` single-client, so nothing else can attach to it.
- Allen's product direction: Jarvis holds the conversation and the memory;
  execution is delegated; modes and proactive delivery stay as future slots.

## Decision

Jarvis stops judging what a worker did. A worker is a Codex app-server thread
driven by four tools — spawn, wait, send input, close — and the parent model
reads one final message. Tools are one flat definition whose handler is a
function of its arguments; the dispatcher owns every event, lifecycle and
serialization step. Allen's own desktop Codex sessions are observed through
`~/.codex/hooks.json`, never attached to. The six layers, the import gate,
the action pipeline, the confirmation gate, and the mode and notify slots are
unchanged.

## Alternatives rejected

- **Keep the audit chain and only fix the documents** — the chain's own
  events show it fired once in four months; 7k lines for one run.
- **Port Codex's tool model (trait per tool, namespaces, exposure enum,
  approval trait)** — Codex's trivial clock tool is 139 lines end to end;
  Hermes's flat entry is 52 and Jarvis's today is 124. The flat shape is the
  smaller one and already carries the risk level Codex's does not.
- **Switch Allen to terminal `codex` plus `app-server daemon` for full
  push** — the desktop hooks already deliver approval requests (answerable)
  and the final report; the only capability the daemon adds is mid-turn
  free-form input.
- **Parse the worker's final message into a structured report** — that is
  the audit chain re-entering through the back door; Codex itself hands the
  parent an untyped string capped at 900 tokens.

## Consequences

- Worker quality is Codex's. Jarvis has no verdict of its own on a diff and
  will speak whatever the worker reports.
- The app-server v2 protocol is partly experimental across 0.142 → 0.155;
  the CLI version becomes a pinned external dependency.
- `docs/spec.html` §7, §8, §13 shrink to what remains built and §15, §16 go;
  the spec must move with the code or it reads as contract again.
- A running desktop session cannot be steered mid-turn from Jarvis; only the
  `Stop` and `UserPromptSubmit` hooks carry text back into it.
