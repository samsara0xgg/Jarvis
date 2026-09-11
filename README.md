# Jarvis

Allen's state-centric personal runtime.

Canonical specification: [`docs/spec.html`](docs/spec.html).

Interactive diagrams: [runtime architecture](docs/archify/jarvis.architecture.html)
and [task execution / verification](docs/archify/task-run.sequence.html).
See [Archify usage and source evidence](docs/archify/README.md) to update them.

## Layers

```
jarvis/
├── constitution/   L1
├── state/          L2
├── decision/       L3
├── execution/      L4
├── surface/        L5
├── deployment/     L6
├── runtime/        composition root
├── shared/         primitives
└── cli/            entry points
```

Dependency direction is top-to-bottom. The four middle layers (decision /
execution / surface / deployment) are independent siblings — they cannot
import each other. `runtime/` is the only place allowed to wire across.
Enforced by [`.importlinter`](./.importlinter).
