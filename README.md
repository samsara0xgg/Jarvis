# Jarvis

Allen's state-centric personal runtime.

> Identity lives in a durable **State Object** — not in the LLM, prompt, mode,
> tools, or voice pipeline. Surfaces are replaceable; continuity is not.

---

## What Jarvis is (and is not)

**Is:** an N=1 personal runtime that reduces Allen's context-reload cost —
remembers what's running, what's open, what's verified vs reported, what's
drifting. Acts as a **supervisor** that schedules / verifies / consolidates;
delegates heavy execution to worker agents (Codex / Claude Code / Hermes).

**Is not:** a chatbot, voice assistant, Home Assistant replacement, generic
agent framework, IDE competitor, or multi-user SaaS.

See [`docs/spec.html`](docs/spec.html) for the canonical specification.

---

## 6-Layer Architecture

| Layer | Role |
| --- | --- |
| **L1 Constitution** | Identity, immutable principles (C1–C6), 5 autonomy axes, 8 cross-layer invariants. Imports nothing. |
| **L2 State Object** | Event Log (append-only) + Projections (read-only fold). The single source of persistent truth. |
| **L3 Runtime Decision** | Reads projection snapshots, assembles Situation Packet, resolves Effective Policy, routes intent, gates actions, interprets results. |
| **L4 Capability Execution** | Executes gated `ActionRequest`s through tools / adapters / workers. Cannot self-verify. |
| **L5 Surface / IO** | World ↔ Jarvis boundary. Input canonicalisation, output rendering, channel selection, freshness UX. |
| **L6 Deployment** | Mac single domain (for now). Sleep/wake protocol, deferred execution via launchd, artifact store. RPi reserved for future. |

Dependency direction is strictly top-to-bottom and enforced by
[`.importlinter`](./.importlinter) in CI.

---

## Directory Tour

```
jarvis/                Python package — one folder per layer + runtime/shared/cli
├── constitution/      L1
├── state/             L2: events/  projections/  claims/  store/
├── decision/          L3: packet/  policy/  routing/  attention/  gates/  ...
├── execution/         L4: registry/  adapters/  sandbox/  lifecycle/  tools/
├── surface/           L5: contracts/  inputs/  outputs/  routing/  view_model/
├── deployment/        L6: domains/  sleep_wake/  scheduler/  artifact_store/
├── runtime/           composition root — the only cross-layer wiring
├── shared/            primitives (IDs, time, refs, errors). Keep minimal.
└── cli/               entry points

tests/                 mirrors jarvis/ structure (unit/, contract/)
system_tests/          end-to-end scenarios (runner + suites/)
ui/                    non-Python frontends (inherent visual cockpit, desktop pet)
config/                jarvis.yaml, modes/, schemas/
docs/                  spec.html (canonical), adrs/, verification log
scripts/               ops + dev scripts (replay, rebuild, backup)
var/                   runtime state (mac_events.db, artifacts, logs) — gitignored
```

Models live in `~/Models/` (outside the repo). Configure paths in
`config/jarvis.yaml`.

---

## Architecture Rules (read before adding code)

1. **Dependency direction is one-way.** Lower layers never import higher
   layers. The middle four (`decision`, `execution`, `surface`, `deployment`)
   are independent siblings — they cannot import each other.
2. **`shared/` is minimal.** Only true cross-layer primitives. If it's a
   domain concept (event, claim, packet, action), it belongs in its layer.
3. **Event schemas live only in `state/events/types/`.** Any other layer
   defining its own event payload is an architectural violation.
4. **Tools are organised by `caller_principal`, not by functional domain.**
   Drop a new tool into `execution/tools/<principal>/`, not `tools/git/`.
5. **L5 strict separation:** `inputs/` only emit canonical events, `outputs/`
   only render approved `ResponsePlan`s. Attention / policy / verification
   decisions belong in L3.

These rules are partially enforceable by `import-linter` (run via `lint-imports`
in CI).

---

## Quickstart

```bash
# install (mise + uv per ~/.claude/CLAUDE.md)
mpy                                    # bootstrap Python 3.12 + uv venv
uv pip install -e ".[dev]"             # base + dev deps

# tests
pytest -q                              # unit + contract tests
lint-imports                           # verify layer boundaries

# run (commands will land as layers come online; see docs/adrs/)
jarvis dev                             # text-input dev loop (M2+)
jarvis                                 # full voice runtime (M4+)
jarvis inspect projection <name>       # query a projection from CLI
jarvis replay --from <event_uid>       # replay event log
```

---

## Roadmap

Milestones unlock **architectural capabilities**, not user-visible features:

- **M0** — Event Type Registry + Projection contracts frozen (docs only)
- **M1** — L2 spine: Event Log + minimum projections; all side-effects emit events
- **M2** — L3 decision loop explicit: Situation Packet + ActionRequest + result_semantics
- **M3** — Claim/Evidence + Pre-emit Gate v0 + Worker `submit_report` + Action 8-state lifecycle
- **M4** — Inherent visual cockpit (L5 second channel)
- **M5** — Memory promotion (4-question filter)
- *(M6 RPi domain — deferred until Mac-only loop is stable.)*

The first usable end-to-end scenario (target after M3):
> "派 Codex 干活，回来后独立验证再如实汇报。"
> Spawn Codex on a task; verify result independently; respond honestly about
> what was verified vs reported.

See [`docs/adrs/`](docs/adrs/) for architecture decision records.

---

## Project Conventions

- Python 3.12, type hints everywhere, Pydantic v2 for all schemas
- Google-style docstrings
- Module headers state which layer they belong to, what they own, what they don't
- Tests mirror source tree (`jarvis/state/events/log.py` ↔ `tests/unit/state/events/test_log.py`)
- Commits: `type(scope): English description`, one logical change per commit
- Never push without explicit ask. No Co-Authored-By trailer.
- See [`CLAUDE.md`](CLAUDE.md) for the full set of working rules.
