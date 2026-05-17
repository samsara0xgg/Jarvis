# Jarvis — Allen 的私人 state-centric runtime

## Rules

- Answer first, explore later. Discuss first, write code later.
- Run `pytest -q` only after a major change. `lint-imports` must pass on every commit.
- Commit OK, **never push** unless explicitly asked.
- **No Co-Authored-By** in commit messages.
- Use Grep / Glob before spawning Agents.
- Don't hardcode IPs, API keys, model paths — read from `config/jarvis.yaml`.
- Don't use `print` — use the logger in `jarvis/runtime/observability/logging.py`.
- Don't violate layer boundaries (see Architecture below). `lint-imports` will
  block you, but understand the rule before reaching for an `ignore`.

## Quick Reference

```bash
# Setup (one-time)
mpy                                    # Python 3.12 + uv venv (per ~/.claude/CLAUDE.md)
uv pip install -e ".[dev,voice,inherent]"

# Develop
pytest -q                              # unit + contract tests
lint-imports                           # layer dependency check
ruff check jarvis tests                # lint
mypy jarvis                            # type-check

# Run (these land as milestones complete — see README roadmap)
jarvis dev                             # text-input dev loop  (M2+)
jarvis                                 # full voice runtime   (M4+)
jarvis inspect projection task_ledger  # query a projection
jarvis replay --from <event_uid>       # replay event log
```

## Architecture (read `docs/spec.html` for the canonical version)

```
L1 Constitution     identity, principles, invariants  (imports nothing)
L2 State Object     event log + projections           (the spine)
L3 Runtime Decision packet / policy / routing / gates (judgement)
L4 Capability Exec  tool registry / sandbox / worker  (hands)
L5 Surface / IO     voice / inherent / observers      (boundary)
L6 Deployment       Mac single domain, sleep/wake     (substrate)
```

Dependency direction is strictly top-to-bottom. `decision / execution / surface
/ deployment` are independent siblings — they cannot import each other.
`runtime/` is the only place allowed to wire across layers.

## Hard Rules When Adding Code

1. **New event type** → add Pydantic model under `jarvis/state/events/types/<family>.py`,
   register in `jarvis/state/events/registry.py`. No layer outside `state/events/`
   may define event payloads.
2. **New tool** → drop into `jarvis/execution/tools/<principal>/`, where
   `<principal>` is the default `caller_principal` (observer / worker / verify /
   write / system). Never group by functional domain (no `tools/smart_home/`).
3. **New projection** → add under `jarvis/state/projections/`, inherit from
   `projections/base.py`, implement fold + rebuild.
4. **New surface input** → add under `jarvis/surface/inputs/`. Must only produce
   canonical events; never make intent decisions.
5. **New surface output** → add under `jarvis/surface/outputs/`. Must only render
   approved `ResponsePlan` / `PresentationIntent`; never decide attention.
6. **Cross-layer wiring** → only in `jarvis/runtime/compose.py`. If you feel
   the urge to wire elsewhere, the code is in the wrong layer.
7. **New ML / heavy dep** → add to a `[project.optional-dependencies]` group
   in `pyproject.toml`. Base install stays light.

## Coding Standards

- Type hints everywhere (mypy `strict = true`)
- Google-style docstrings on public classes / functions
- Pydantic v2 for every schema (events, packets, ActionRequest, ToolDefinition,
  ResponsePlan, AuthorizationLease — all of them)
- One class per file when the class is non-trivial; otherwise group cohesively
- File header docstring: state which layer, what it owns, what it does not own
- New skills inherit base contracts — never bypass `ToolDefinition` /
  `AuthorizationLease` / `SandboxPolicy`
- Graceful degradation when hardware unavailable (mic / models / network)

## Git

- Commit message: `type(scope): English description`,
  type ∈ `feat fix refactor test docs chore perf data`
- One logical change per commit. No `git add .` / `-am`混提交 / `push -f` / `--no-verify`
- 大重构走 `feat/xxx` branch；小改动直接 `main`
- 未人工验证的改动在 commit body 加 `Verify:` trailer

## System Tests

After changes that affect runtime behaviour (skip for pure refactors):

1. Read the diff, draft 3-4 prompts that exercise the change.
2. **Present prompts to Allen and wait for explicit approval before running.**
3. On approval, run `system_tests/runner.py` (once it exists post-M2).
4. Parse JSON, fix `failures` automatically, batch `needs_review` items.

## Stack

Python 3.12 · `config/jarvis.yaml` for all runtime settings
- ASR: SenseVoice-Small INT8 (sherpa-onnx) + Whisper fallback
- VAD: Silero
- Wake: openwakeword (hey_jarvis_v0.1)
- LLM (Tier 2 cloud): gpt-5.4-mini default, swappable
- TTS: MiniMax speech-2.8-turbo (primary + fallback endpoints)
- Storage: SQLite WAL for event log + projections; artifact store on local FS
- Frontends: `ui/inherent/` (browser visual cockpit) + `ui/desktop/` (Electron pet mode)

Models live in `~/Models/` (outside the repo). Config paths in `config/jarvis.yaml`.

## Obsidian Wiki

继承全局协议（`~/.claude/CLAUDE.md` + `~/Documents/Obsidian Vault/_SCHEMA.md`）。
项目 vault：`~/Documents/Obsidian Vault/jarvis/`（根目录即 jarvis vault，无子目录）。
开场静默读 `{vault}/_hot.md`；`/clear` / "收尾" / 30+ 轮时主动提议写 `sessions/`。

Canonical spec lives at `docs/spec.html` (mirror of vault `_spec.html`).
