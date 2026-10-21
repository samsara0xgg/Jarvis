# ADR 0370 — Config is parsed once into a typed RuntimeConfig at entry

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

Read from the code path on this tree (cfb1b636), not from a plan.

`config/jarvis.yaml` is merged with `<root>/settings.yaml` and the Settings page's store (ADR 0052),
and the result travelled as a `dict`. Every reader took the mapping and went to the key itself:

- `jarvis/runtime/__init__.py` held 43 private functions that take the mapping, each with its own
  default and its own `RuntimeBootstrapError` text, plus the same reads again in `inherent_loop.py`,
  `daemon/*` and `terminal.py`. A key had as many defaults as it had readers.
- Derived values were recomputed where they were needed: the repo root four ways
  (`_locate_repo_root`, `config_path.resolve().parent.parent`, the daemon's `launchd.repo_root()`,
  and `parents[2]` in `decision.pricing_path`); the pricing path by `repo_root / "data" /
  "pricing.json"` thirteen times across bootstrap and serve; the owner's zone by
  `resolve_zone(None, ...)` eighteen times across seven modules of `jarvis/runtime/`.
- The response-flag graph (the `realtime.enabled` parent over the streaming children) was resolved
  at two sites, `bootstrap_runtime_app` and the terminal's `_start_speech`, and each one logged the
  downgrade warning and recorded the trace itself, so how often a downgrade was reported depended
  on which sites a process reached and how often it reached them.
- Validation ran in the middle of `bootstrap_runtime_app`, after the Event Log was open and the
  lock taken. A malformed value stopped the boot with resources already held, and a reader that ran
  only on a path (the terminal, `serve`'s late steps) found its bad value only when that path ran.

Constraints: the error messages and exception types are the tests' and the owner's interface
(`RuntimeBootstrapError` for a refused value, `ValueError` for a zone name that is not IANA), and
`lint-imports` keeps L2..L5 from reading `runtime/`'s config: the parse stays in `runtime/`, and
what a lower layer needs is handed to it as plain values.

## Decision

`jarvis.runtime.config.load_runtime_config` parses and validates the merged config once, at the
entry point, into a frozen `RuntimeConfig` that is passed down. A setting that has a field is read
from the field; the serve, terminal and CLI entry points take the `RuntimeConfig` instead of a
dict, and read `raw` only for what is listed below.

The tree is frozen dataclasses with the defaults the readers had. It owns the single repo root,
the pricing path, the owner's zone, the role's overrides (ADR 0170) and the response-flag graph,
each resolved once. Entry order is: lay out the root, load its environment, parse the config, open
resources. A refused value raises before the Event Log is opened or a thread started. `raw`, the
merged mapping, stays on the tree for what addresses settings by key path (the Settings page, ADR
0227's drift check) and for blocks whose own layer parses them whole (`llm`, the voice stack's
knobs, `tools.mcp` and `tools.plugins`, the `open_path` bookmarks); it is not a second way to read
a setting that has a typed field.

Moved into the tree: the role, listen addresses and hosts, language, assistant name, models, the
memory and session blocks, `realtime` (switches, flag graph, pump bounds, service tier, ambient
sounds), `confirmation`, `voice`, `llm` tool-iteration bounds, `observer`, `supervisor`,
`work_state`, `daily_report`, `locate`, `home.weather`, `transit`, `maps`, `dashboard`,
`diagnostics`, `voiceprint.gate`, `commitments.remind_before_min`, `day_plan.day_ends`, the built-in
`tools` (web, screen, vault, workers), Jev's routes (`jev_log`, `realtime.surrogate_route`,
`realtime.jev_words`, `realtime.jev_oneshot`, `agents.turn_end_asks`), the mail and record intakes
(`home.mail_reply`, `job_mail`, `mail_codes`, `commitments`, `claude_daily`, `turn_things`) and the
day's blocks (`moment`, `night`, `ledger`, `doing`), plus the key files and ids of `push` and
`weather.weatherkit`, which stay text on the tree: the key is read where the client is built. Each
keeps the default, the error text and the exception type it had: `RuntimeBootstrapError` for a
refused value, `TypeError` for the two bool switches that always raised it, `ValueError` for the
timesink path and the zone name.

Not moved: `projects`, which the terminal tolerates malformed (its own catalog is a warning, the
brain's is a refusal) where a tree field would stop the terminal from starting.

Secrets are not in the tree: a key is resolved from the environment where the thing that needs it
is built.

## Alternatives rejected

- **pydantic models.** `pyproject.toml` does not declare pydantic; `uv.lock` has it only as a
  dependency of anthropic, fastapi, mcp and openai. Its `ValidationError` text differs from the
  messages above, so every test that matches a message would be rewritten and the owner's error
  text would change with no behaviour gained. The existing validators are plain functions over
  YAML scalars with fallbacks; moving them into dataclass parsers keeps their bodies, and a model
  layer would replace them rather than move them. A new direct dependency for this was not
  justified.
- **Keep the readers, cache their results.** The 43 functions stay and a `functools.cache` makes
  them cheap. It leaves a key with many defaults and puts validation where it was, in the middle of
  boot; the duplication this ADR removes is in the defaults and the order, not in the cost.
- **A module-level config singleton.** Readers import it instead of receiving it. Tests that boot
  two runtimes in one process over two roots (`test_terminal_observers` boots a one-machine runtime
  and a brain side by side) would share it, and an import-time parse would raise before an entry
  point could report.
- **Parse lazily per section.** Each section parsed on first use is what the readers did; it is
  why a bad value surfaced only when its path ran.

## Consequences

An entry point that does not read a section now fails on that section being malformed: the
terminal and the one-off CLI commands (`mcp-login`, `import`, `voiceprint`) refuse a bad
`job_mail` block they never use. The Settings page's store is applied for those commands too,
where before only the daemon applied it.

A new setting is a field on the tree and a line in its parser; a reader that goes to `raw` for
a key that has a field is a defect a review should catch, and the remaining `raw` readers are the
ones that address by key path, hand a block to its layer, or are the unmoved feature builders
listed under Decision.

Tests that build a runtime by hand pass `runtime_config(mapping)` (`tests/support/runtime_config.py`)
instead of a dict, and change a section with `reconfigure`; a test of a refused value goes through
`load_runtime_config`, not through the private parser.

`serve --check-config` and `terminal --check-config` load the config through `load_runtime_config`
and nothing else of the entry point, so a deploy can refuse a root whose settings the stricter parse
rejects before it restarts; they cover the load, not the feature builders listed above, which still
validate when bootstrap assembles them.

L6's launchd agent still finds the repo root itself: its install and status commands do not load a
config, and `deployment` cannot import `runtime`. Passing the root in would make every launchd
command load the config first, which is not a small change.
