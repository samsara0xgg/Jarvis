---
paths:
  - "**/*.py"
---

# Python verification

- Tests are sorted by level (ADR 0325): static, behaviour table, component,
  contract, end-to-end smoke, eval. Assert what the owner or another app can
  observe: event-log rows, HTTP/WebSocket payloads, ledger state, return
  values. Never private attributes or log wording; user-visible strings by
  their `lang.py` key.
- No real sleep and no wall-clock assertion in a new test: time comes from an
  injected clock and zone (`tests/support/clock.py`).
- Fakes and shared helpers live in `tests/support/`; a test file never imports
  another test file. Replace the model only through the one shared fake
  (`tests/support/model.py`), never by patching `decide` (ADR 0326).
- A bug fix adds a row to the table that owns its behaviour class.
- The hermetic suite runs whole in CI on every push to main
  (`.github/workflows/ci.yml`, UTC and America/Vancouver). A red main is fixed
  by the thread that pushed it.
- A new capability whose correctness depends on a real LLM or a real runtime
  interaction needs a live run before it counts as done. Live runs are not
  gated on cost.
- Small fixes and refactors do not automatically need a live test; choose
  verification by regression risk.
- Tier 1 gates are `lint-imports`, `ruff`, `mypy --strict`, the hermetic
  acceptance checks, and `uv audit` (known vulnerabilities in `uv.lock`).
  Report their printed counts; never infer them.
