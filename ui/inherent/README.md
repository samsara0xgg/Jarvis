# ui/inherent/

Browser-based visual cockpit — Jarvis's second L5 channel after voice.

Read-only window into State Object: active task, open actions, agent runs,
mode, claim/evidence, freshness, stale state. UI button interactions emit
`surface.user_intent` events through the Python server in
`jarvis/surface/outputs/inherent/server.py`; the frontend never owns truth.

## Status

Scaffold only. Lands at **M4** ([roadmap in README.md](../../README.md#roadmap)).
The frontend toolchain (React / Vite / etc.) and the server endpoints are
both deferred until then.

## Planned layout

```
src/         frontend source
dist/        build artefacts (gitignored)
package.json built independently of the Python package
```
