# system_tests/

End-to-end scenario suites that exercise the full 6-layer stack.

Unlike `tests/` (which runs unit + contract tests via pytest in milliseconds),
these scenarios talk to a running Jarvis process, drive real input adapters,
and assert on emitted events + projection state.

## Layout

- `suites/` — YAML scenario definitions, one file per scenario family
- `reports/` — JSON output of past runs (gitignored)

## Runner (lands at M2)

```bash
python -m system_tests.runner --mode cc --suite <name>
python -m system_tests.runner --mode cc --prompts "<p1>|<p2>|..."
python -m system_tests.runner                    # human interactive
```

## Workflow (from CLAUDE.md)

After changes that affect runtime behaviour (skip for pure refactors):
1. Read the diff, draft 3-4 prompts that exercise the change
2. Present prompts to Allen and wait for explicit approval before running
3. On approval, invoke the runner
4. Parse JSON, fix `failures` automatically, batch `needs_review` to Allen
