---
paths:
  - "**/*.py"
---

# Python verification

- `tests/unit` is retired. Do not add new Python unit tests.
- Verify with the task's acceptance command, data-driven input to expected
  output checks, canaries, regression pins, integration and scenario
  harnesses, and the required live tests.
- A new capability whose correctness depends on a real LLM or a real runtime
  interaction needs a live run before it counts as done. Live runs are not
  gated on cost.
- Small fixes and refactors do not automatically need a live test; choose
  verification by regression risk.
- Tier 1 gates are `lint-imports`, `ruff`, `mypy --strict`, and the hermetic
  acceptance checks. Report their printed counts; never infer them.
