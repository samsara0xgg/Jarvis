# Tier 0 Regex Router (Batch 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the empty Tier 0 scaffold (`tier_0_match` always-None) into a real spec §17 deterministic fast path — a file-editable regex whitelist that dispatches L4 tools with `caller_principal=regex_router` through the full gate/audit chain, bypassing only the LLM — shipping `get_current_time` plus two patterns (现在几点 / 今天几号).

**Architecture:** A new pure L3 module `jarvis/decision/tier0.py` owns pattern loading (`config/tier0_patterns.yaml`), validation, matching, and deterministic response rendering. `decide()`'s `_handle_utterance` branch gains a Tier 0 hit path that mirrors `_dispatch_one_tool_call`'s pipeline (action.proposed → Pre-action Gate → action.authorized → L4 dispatch → Result Interpreter) and finalizes through the existing `_finalize_response` (Pre-emit Gate + attention policy). Spec conformance was pre-verified: §3.5.2 pins "regex_router — 低延迟路径，但仍要过 entity / policy / risk gate"; §17 pins "Tier 0 命中 → 直接 tool_registry.execute"; §14.5 pins "regex_router tiny L0-L1 不升级". The YAML file is routing data, not a capability grant — the registry `allowed_callers` filter, the Pre-action Gate `caller_allowed` check, and the L4 dispatch re-check remain the three code-side enforcement layers.

**Tech Stack:** Python 3.12, stdlib `re` + PyYAML (already a dependency — precedent: `jarvis/decision/llm.py:34` imports yaml in L3), pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/spec.html` §17 (Intent Routing), §14.3/§14.5 (regex_router surface + caller×layer defaults), §3.5.2 (caller principals), §3.4.9 (read-only lifecycle), §3.4.12 (Pre-emit Gate). Design conformance review: conversation 2026-08-24 (Tier 0 batch 1, approved by Allen).

## Global Constraints

- Tier 1 gates must stay green after every task: `lint-imports` KEPT · `ruff check` clean · `mypy --strict` clean · all unit tests pass · unit suite wall < 30s.
- Test/gate commands run with the main repo venv python: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit/... -v` (worktree cwd shadows the venv's package — verified this session).
- Layer rules: `jarvis.decision.*` imports stdlib + `jarvis.shared` (+ `yaml`, precedent `llm.py`) only; never sibling layers (`execution`/`surface`/`deployment`). Only `jarvis/runtime/` wires across layers.
- All Tier 0 patterns MUST be full-sentence anchored `^...$` (spec §17) — the loader rejects unanchored patterns.
- Response templates MUST NOT contain completion-class keywords (完成 / 已完成 / done / verified) — they would trip the Pre-emit Gate (`jarvis/decision/gates.py` `_COMPLETION_KEYWORDS`).
- Commits: Conventional Commits, house 5-part body (see `CLAUDE.md`), NO `Co-Authored-By`, no `git add .`/`-A`. All commits land on branch `worktree-tier0-regex-router`; NEVER push; Allen reviews before any merge to main.
- Event payloads: `action.proposed` required keys are `action_id, tool_name, caller_principal, risk_level`; `action.result_observed` required keys are `action_id, semantics`. Unknown extra keys are accepted Day-1 (`jarvis/state/event_log.py` emit_event docstring) — `routed_by`/`pattern_id` ride as extras, no registry change.

---

### Task 1: `jarvis/decision/tier0.py` — pattern table: load / validate / match / render

**Files:**
- Create: `jarvis/decision/tier0.py`
- Test: `tests/unit/test_tier0.py`

**Interfaces:**
- Consumes: nothing from other tasks (pure module: stdlib `re`, `yaml`, dataclasses).
- Produces (later tasks rely on these exact names):
  - `class Tier0ConfigError(ValueError)`
  - `@dataclass(frozen=True) Tier0Pattern` — fields `pattern_id: str`, `regex: re.Pattern[str]`, `tool_name: str`, `arg_template: Mapping[str, str]`, `response_template: str`
  - `Tier0Table = tuple[Tier0Pattern, ...]` (type alias)
  - `@dataclass(frozen=True) Tier0Hit` — fields `pattern_id: str`, `tool_name: str`, `tool_args: Mapping[str, str]`, `response_template: str`
  - `load_tier0_table(path: Path) -> Tier0Table` (missing file → `()`)
  - `validate_tier0_table(table: Tier0Table, *, allowed_tool_names: frozenset[str], async_tool_names: frozenset[str]) -> None`
  - `match_tier0(transcript: str, table: Tier0Table) -> Tier0Hit | None`
  - `render_tier0_response(hit: Tier0Hit, payload: Mapping[str, Any]) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_tier0.py`:

```python
"""Unit tests for the Tier 0 pattern table (spec §17 closed whitelist)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.decision.tier0 import (
    Tier0ConfigError,
    load_tier0_table,
    match_tier0,
    render_tier0_response,
    validate_tier0_table,
)

if TYPE_CHECKING:
    from pathlib import Path

_VALID_YAML = """\
- id: time_now
  pattern: "^现在几点了?[?？。.!！]?$"
  tool: get_current_time
  template: "现在是{spoken_time}。"
- id: capture_demo
  pattern: "^加个待办[:：]\\\\s*(.+)$"
  tool: create_task
  args: { goal: "$1" }
  template: "记下了：{goal}。"
"""


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "tier0_patterns.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_missing_file_returns_empty_table(tmp_path: Path) -> None:
    assert load_tier0_table(tmp_path / "nope.yaml") == ()


def test_load_valid_file_two_patterns(tmp_path: Path) -> None:
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    assert [p.pattern_id for p in table] == ["time_now", "capture_demo"]
    assert table[0].tool_name == "get_current_time"
    assert table[1].arg_template == {"goal": "$1"}


def test_load_rejects_unanchored_pattern(tmp_path: Path) -> None:
    bad = '- id: x\n  pattern: "现在几点"\n  tool: get_current_time\n  template: "t"\n'
    with pytest.raises(Tier0ConfigError, match="x.*anchored"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_bad_regex(tmp_path: Path) -> None:
    bad = '- id: x\n  pattern: "^([$"\n  tool: get_current_time\n  template: "t"\n'
    with pytest.raises(Tier0ConfigError, match="x"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_duplicate_id(tmp_path: Path) -> None:
    bad = (
        '- id: x\n  pattern: "^a$"\n  tool: t1\n  template: "t"\n'
        '- id: x\n  pattern: "^b$"\n  tool: t1\n  template: "t"\n'
    )
    with pytest.raises(Tier0ConfigError, match="duplicate"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_missing_keys(tmp_path: Path) -> None:
    bad = '- id: x\n  pattern: "^a$"\n'
    with pytest.raises(Tier0ConfigError, match="x"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_non_list_document(tmp_path: Path) -> None:
    with pytest.raises(Tier0ConfigError, match="list"):
        load_tier0_table(_write(tmp_path, "key: value\n"))


def test_validate_rejects_tool_not_in_allowed_set(tmp_path: Path) -> None:
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    with pytest.raises(Tier0ConfigError, match="capture_demo.*create_task"):
        validate_tier0_table(
            table,
            allowed_tool_names=frozenset({"get_current_time"}),
            async_tool_names=frozenset(),
        )


def test_validate_rejects_async_tool(tmp_path: Path) -> None:
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    with pytest.raises(Tier0ConfigError, match="async"):
        validate_tier0_table(
            table,
            allowed_tool_names=frozenset({"get_current_time", "create_task"}),
            async_tool_names=frozenset({"create_task"}),
        )


def test_validate_passes_clean_table(tmp_path: Path) -> None:
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    validate_tier0_table(
        table,
        allowed_tool_names=frozenset({"get_current_time", "create_task"}),
        async_tool_names=frozenset(),
    )


def test_match_hits_full_sentence_only(tmp_path: Path) -> None:
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    assert match_tier0("现在几点", table) is not None
    assert match_tier0("现在几点了？", table) is not None
    assert match_tier0("  现在几点  ", table) is not None  # strip
    assert match_tier0("请问现在几点", table) is None  # prefix breaks anchor
    assert match_tier0("现在几点了顺便查天气", table) is None
    assert match_tier0("昨天那个 task 给 Codex 跑一下", table) is None


def test_match_extracts_capture_group_args(tmp_path: Path) -> None:
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    hit = match_tier0("加个待办：买牛奶", table)
    assert hit is not None
    assert hit.tool_name == "create_task"
    assert hit.tool_args == {"goal": "买牛奶"}


def test_render_fills_payload_and_args(tmp_path: Path) -> None:
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    hit = match_tier0("现在几点", table)
    assert hit is not None
    text = render_tier0_response(hit, {"spoken_time": "下午3点42分"})
    assert text == "现在是下午3点42分。"


def test_render_missing_variable_falls_back(tmp_path: Path) -> None:
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    hit = match_tier0("现在几点", table)
    assert hit is not None
    text = render_tier0_response(hit, {"unrelated": "x"})
    assert "模板变量缺失" in text
    assert "完成" not in text  # must stay Pre-emit-Gate safe
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit/test_tier0.py -v`
Expected: FAIL / collection error with `ModuleNotFoundError: jarvis.decision.tier0`

- [ ] **Step 3: Implement `jarvis/decision/tier0.py`**

```python
"""L3 Tier 0 deterministic pattern table (spec §17).

The closed regex whitelist lives in ``config/tier0_patterns.yaml`` so
Allen can add patterns without touching code. The file is routing data,
NOT a capability grant — three code-side layers still enforce what the
regex_router principal may dispatch (registry ``allowed_callers``
filter, Pre-action Gate ``caller_allowed`` check, L4 dispatch
re-check). Spec anchors: §17 "Tier 0 命中 → 直接 tool_registry.execute",
§3.5.2 "低延迟路径，但仍要过 entity / policy / risk gate", §14.5
"regex_router tiny L0-L1 不升级".

Layer rules: stdlib + ``yaml`` (L3 precedent: ``jarvis.decision.llm``).
No jarvis imports at all — this module is pure data/logic so the
loader/matcher stay trivially unit-testable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)


class Tier0ConfigError(ValueError):
    """Raised when ``config/tier0_patterns.yaml`` is malformed.

    The composition root converts this into ``RuntimeBootstrapError``
    so a broken pattern file fails the daemon start loudly instead of
    silently dropping entries.
    """


@dataclass(frozen=True)
class Tier0Pattern:
    """One compiled whitelist entry (see module docstring for the file schema)."""

    pattern_id: str
    regex: re.Pattern[str]
    tool_name: str
    arg_template: Mapping[str, str] = field(default_factory=dict)
    response_template: str = ""


Tier0Table = tuple[Tier0Pattern, ...]


@dataclass(frozen=True)
class Tier0Hit:
    """A pattern hit with capture-group args already extracted."""

    pattern_id: str
    tool_name: str
    tool_args: Mapping[str, str]
    response_template: str


_GROUP_REF_RE = re.compile(r"^\$(\d+)$")

_REQUIRED_KEYS = ("id", "pattern", "tool", "template")


def load_tier0_table(path: Path) -> Tier0Table:
    """Parse + structurally validate the YAML whitelist at ``path``.

    Missing file → empty table (Tier 0 disabled, spec §17 Day-1 state).
    Any malformed entry → :class:`Tier0ConfigError` naming the entry —
    never a silent skip.
    """
    if not path.is_file():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return ()
    if not isinstance(raw, list):
        msg = f"tier0 patterns: top-level YAML must be a list, got {type(raw).__name__}"
        raise Tier0ConfigError(msg)

    patterns: list[Tier0Pattern] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            msg = f"tier0 patterns: entry #{index} is not a mapping"
            raise Tier0ConfigError(msg)
        missing = [k for k in _REQUIRED_KEYS if not isinstance(entry.get(k), str)]
        if missing:
            entry_id = entry.get("id", f"#{index}")
            msg = f"tier0 patterns: entry {entry_id!r} missing/non-string keys: {missing}"
            raise Tier0ConfigError(msg)
        pattern_id = entry["id"]
        if pattern_id in seen_ids:
            msg = f"tier0 patterns: duplicate id {pattern_id!r}"
            raise Tier0ConfigError(msg)
        seen_ids.add(pattern_id)
        pattern_src = entry["pattern"]
        if not (pattern_src.startswith("^") and pattern_src.endswith("$")):
            msg = (
                f"tier0 patterns: {pattern_id!r} must be full-sentence "
                f"anchored ^...$ (spec §17)"
            )
            raise Tier0ConfigError(msg)
        try:
            regex = re.compile(pattern_src)
        except re.error as exc:
            msg = f"tier0 patterns: {pattern_id!r} regex does not compile: {exc}"
            raise Tier0ConfigError(msg) from exc
        args_raw = entry.get("args", {})
        if not isinstance(args_raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in args_raw.items()
        ):
            msg = f"tier0 patterns: {pattern_id!r} args must be a str->str mapping"
            raise Tier0ConfigError(msg)
        patterns.append(
            Tier0Pattern(
                pattern_id=pattern_id,
                regex=regex,
                tool_name=entry["tool"],
                arg_template=dict(args_raw),
                response_template=entry["template"],
            )
        )
    return tuple(patterns)


def validate_tier0_table(
    table: Tier0Table,
    *,
    allowed_tool_names: frozenset[str],
    async_tool_names: frozenset[str],
) -> None:
    """Cross-check the table against the registry's regex_router surface.

    ``allowed_tool_names`` comes from
    ``registry.for_caller(CallerPrincipal.REGEX_ROUTER)`` at the
    composition root; a pattern naming any other tool fails fast here
    (defense layer 0 — the Pre-action Gate would refuse it per-call
    anyway).
    """
    for pattern in table:
        if pattern.tool_name not in allowed_tool_names:
            msg = (
                f"tier0 patterns: {pattern.pattern_id!r} targets tool "
                f"{pattern.tool_name!r} which regex_router may not call "
                f"(allowed: {sorted(allowed_tool_names)})"
            )
            raise Tier0ConfigError(msg)
        if pattern.tool_name in async_tool_names:
            msg = (
                f"tier0 patterns: {pattern.pattern_id!r} targets async tool "
                f"{pattern.tool_name!r}; Tier 0 dispatches sync tools only"
            )
            raise Tier0ConfigError(msg)


def match_tier0(transcript: str, table: Tier0Table) -> Tier0Hit | None:
    """Return the first whitelist hit for ``transcript``, or None.

    First-match-wins in file order (legacy ``regex_router.py``
    semantics); patterns are ``^...$`` anchored so ordering only
    matters for deliberately overlapping entries.
    """
    text = transcript.strip()
    if not text:
        return None
    for pattern in table:
        m = pattern.regex.match(text)
        if m is None:
            continue
        args: dict[str, str] = {}
        for key, value in pattern.arg_template.items():
            group_ref = _GROUP_REF_RE.match(value)
            if group_ref is not None:
                args[key] = (m.group(int(group_ref.group(1))) or "").strip()
            else:
                args[key] = value
        return Tier0Hit(
            pattern_id=pattern.pattern_id,
            tool_name=pattern.tool_name,
            tool_args=args,
            response_template=pattern.response_template,
        )
    return None


def render_tier0_response(hit: Tier0Hit, payload: Mapping[str, Any]) -> str:
    """Fill the hit's template from tool payload scalars + captured args.

    A template referencing a variable that neither the payload nor the
    args provide must not crash the turn — fall back to a fixed
    limitation-phrased line (no completion-class keywords, so the
    Pre-emit Gate passes it unchanged).
    """
    variables: dict[str, str] = {
        key: str(value)
        for key, value in payload.items()
        if isinstance(value, (str, int, float))
    }
    variables.update(hit.tool_args)
    try:
        return hit.response_template.format(**variables)
    except (KeyError, IndexError) as exc:
        LOGGER.warning(
            "tier0 render: template for %r missing variable %s", hit.pattern_id, exc,
        )
        return f"指令 {hit.pattern_id} 已执行，但响应模板变量缺失。"


__all__ = [
    "Tier0ConfigError",
    "Tier0Hit",
    "Tier0Pattern",
    "Tier0Table",
    "load_tier0_table",
    "match_tier0",
    "render_tier0_response",
    "validate_tier0_table",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit/test_tier0.py -v`
Expected: all PASS

- [ ] **Step 5: Run Tier 1 gates on the touched files**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m ruff check jarvis/decision/tier0.py tests/unit/test_tier0.py && /Users/alllllenshi/Projects/jarvis/.venv/bin/python -m mypy --strict jarvis/decision/tier0.py && /Users/alllllenshi/Projects/jarvis/.venv/bin/python -m lint_imports`
Expected: clean / KEPT (use the repo's canonical gate invocations if they differ — check `pyproject.toml` tool sections on first run)

- [ ] **Step 6: Commit**

```bash
git add jarvis/decision/tier0.py tests/unit/test_tier0.py
git commit -m "feat(decision): Tier 0 pattern table — load/validate/match/render

Batch 1 of the spec §17 Tier 0 regex fast path.

- jarvis/decision/tier0.py — YAML whitelist loader (full-anchor
  enforcement, fail-fast Tier0ConfigError), registry cross-validator,
  first-match-wins matcher with \$N capture-group args, deterministic
  template renderer with Pre-emit-safe fallback.
- tests/unit/test_tier0.py — 15 LLM-free tests: schema errors,
  anchoring, duplicate ids, allowed/async tool validation, full-
  sentence matching, arg extraction, render fallback.

Tier 1: lint-imports KEPT · ruff clean · mypy strict clean · unit
tests pass · wall <t>s (< 30s budget).

Legacy consulted: jarvis-legacy/core/regex_router.py (pattern table
shape, first-match semantics; config-driven aliases idea)."
```

---

### Task 2: `get_current_time` L4 tool

**Files:**
- Modify: `jarvis/execution/tools.py` (handler near `list_tasks_handler` ~line 1566; registration in `build_default_registry` ~line 1995)
- Test: `tests/unit/test_get_current_time.py`

**Interfaces:**
- Consumes: existing `tools.py` internals — `_get_running_event_uid(conn, action_id)`, `tool_result(payload)`, `emit_event`, `RawResult`, `ToolDefinition`, `ToolRegistry.register`.
- Produces: registered tool `"get_current_time"` with `allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM})`, `risk_level="L0"`, `result_semantics="observation"`, `is_async=False`; payload keys `iso`, `date`, `time`, `weekday`, `spoken_time`, `spoken_date` (Task 4's shipped templates use `spoken_time`/`spoken_date`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_get_current_time.py` (harness copied from `tests/unit/test_list_tasks.py` — `bootstrap_runtime` + `open_event_log` + `_seed_lifecycle`):

```python
"""Unit tests for the `get_current_time` L4 read-only tool (Tier 0 batch 1)."""

from __future__ import annotations

import json
from contextlib import closing
from typing import TYPE_CHECKING

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    from pathlib import Path


def _request(caller: CallerPrincipal) -> ActionRequest:
    return ActionRequest(
        action_id="A_gct1",
        tool_name="get_current_time",
        target_entity_ref=None,
        caller_principal=caller,
        risk_level="L0",
        arguments={},
        authorization_lease=None,
        run_id=None,
        turn_id="T_gct1",
    )


def test_get_current_time_observation_flow(tmp_path: Path) -> None:
    paths = bootstrap_runtime(root=tmp_path)
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    lifecycle.register("A_gct1")
    lifecycle.transition("A_gct1", "authorized")
    with closing(open_event_log(paths.event_log)) as conn:
        bundle = registry.dispatch(
            _request(CallerPrincipal.REGEX_ROUTER), conn, paths, lifecycle,
        )
        slot = bundle.slots[0]
        assert slot.semantics == "observation"
        assert slot.error is None
        for key in ("iso", "date", "time", "weekday", "spoken_time", "spoken_date"):
            assert isinstance(slot.payload[key], str) and slot.payload[key]
        assert slot.payload["weekday"].startswith("周")
        # handler emitted its own action.result_observed + finished lifecycle
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='action.result_observed'",
        ).fetchone()
        assert int(row[0]) == 1
        assert lifecycle.state_of("A_gct1") == "result_observed"
        # tool_output is the house JSON envelope
        assert json.loads(slot.tool_output)["date"] == slot.payload["date"]


def test_get_current_time_allows_regex_router_and_llm_only(tmp_path: Path) -> None:
    del tmp_path
    registry = build_default_registry()
    names_for = {
        p: {t.name for t in registry.for_caller(p)} for p in CallerPrincipal
    }
    assert "get_current_time" in names_for[CallerPrincipal.REGEX_ROUTER]
    assert "get_current_time" in names_for[CallerPrincipal.JARVIS_LLM]
    assert "get_current_time" not in names_for[CallerPrincipal.WORKER_AGENT]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit/test_get_current_time.py -v`
Expected: FAIL with `UnknownToolError` (or KeyError) — tool not registered.

- [ ] **Step 3: Implement handler + registration in `jarvis/execution/tools.py`**

Add near the other handler sections (after `list_tasks_handler`). `datetime` may already be imported at module top — check and reuse; otherwise add `from datetime import datetime`:

```python
# --- get_current_time (F-Tier0) ---------------------------------------------

_WEEKDAYS_ZH: Final[tuple[str, ...]] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_DAY_PERIODS: Final[tuple[tuple[int, str], ...]] = (
    (6, "凌晨"), (9, "早上"), (12, "上午"), (13, "中午"), (18, "下午"), (24, "晚上"),
)


def _spoken_day_period(hour: int) -> str:
    """Map a 24h hour to the zh-CN day-period prefix used by TTS."""
    for upper_bound, label in _DAY_PERIODS:
        if hour < upper_bound:
            return label
    return "晚上"


def get_current_time_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity
    lifecycle: ActionLifecycle,
) -> RawResult:
    """Read the system clock; observation semantics only (spec §3.5.4).

    Zero arguments, zero side effects, risk L0. The Tier 0 regex path
    (spec §17) is the primary caller; jarvis_llm may also call it.
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    now = datetime.now().astimezone()
    hour12 = now.hour % 12 or 12
    weekday = _WEEKDAYS_ZH[now.weekday()]
    payload: dict[str, Any] = {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": weekday,
        "spoken_time": f"{_spoken_day_period(now.hour)}{hour12}点{now.minute}分",
        "spoken_date": f"{now.month}月{now.day}日{weekday}",
    }
    tool_output = tool_result(payload)
    emit_event(
        conn,
        type="action.result_observed",
        payload={
            "action_id": action_request.action_id,
            "semantics": "observation",
            "tool_output": tool_output,
        },
        source_event_id=running_event_uid,
    )
    lifecycle.transition(action_request.action_id, "result_observed")
    return RawResult(
        action_id=action_request.action_id,
        semantics="observation",
        payload=payload,
        tool_output=tool_output,
        error=None,
    )
```

In `build_default_registry()`, after the `list_tasks` registration:

```python
    registry.register(
        ToolDefinition(
            name="get_current_time",
            description="Read the current local date and time (observation only).",
            allowed_callers=frozenset(
                {CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM},
            ),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=get_current_time_handler,
        )
    )
```

Match the surrounding code's exact `Final` import availability / typing style; if `Final` is not imported at top, add it to the existing `typing` import line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit/test_get_current_time.py tests/unit/test_list_tasks.py tests/unit/test_create_task.py -v`
Expected: all PASS (neighbors prove no registry regression).

- [ ] **Step 5: Run Tier 1 gates**

Run: ruff + mypy strict on `jarvis/execution/tools.py tests/unit/test_get_current_time.py`, then `lint-imports`.
Expected: clean / KEPT.

- [ ] **Step 6: Commit**

```bash
git add jarvis/execution/tools.py tests/unit/test_get_current_time.py
git commit -m "feat(execution): get_current_time L0 observation tool

Batch 1 of the spec §17 Tier 0 regex fast path.

- jarvis/execution/tools.py — get_current_time handler (system clock
  observation, zh spoken_time/spoken_date fields for TTS templates)
  + registration with allowed_callers={regex_router, jarvis_llm}.
- tests/unit/test_get_current_time.py — dispatch flow, payload shape,
  lifecycle terminal state, caller-surface scoping.

Tier 1: lint-imports KEPT · ruff clean · mypy strict clean · unit
tests pass · wall <t>s (< 30s budget).

Legacy consulted: jarvis-legacy/tools/time_utils.py (spoken formatting
idea; reshaped to ToolDefinition + RawResult observation contract)."
```

---

### Task 3: decide() Tier 0 hit path

**Files:**
- Modify: `jarvis/decision/intent.py` (replace scaffold `tier_0_match`, ~line 39)
- Modify: `jarvis/decision/__init__.py` (`DecideContext` ~line 519; `_handle_utterance` ~line 670; new `_run_tier0_path` helper next to `_run_tool_use_loop`)
- Test: `tests/unit/test_decide_tier0.py`

**Interfaces:**
- Consumes: Task 1's `Tier0Hit`, `Tier0Table`, `match_tier0`, `render_tier0_response`; Task 2's registered `get_current_time`; existing `_finalize_response(draft_text, packet, ctx, scratch)`, `pre_action_gate`, `result_interpreter`, `_new_action_id()`, `_action_correlation`, `_find_tool_def`, `_latest_event_uid_of_type`, `emit_event`.
- Produces: `DecideContext.tier0_table: Tier0Table | None = None` (Task 4 passes it); `tier_0_match(packet, table=None) -> Tier0Hit | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_decide_tier0.py`. Copy the `_StubRuntimePaths` / `_build_ctx` harness from `tests/unit/test_decision_utterance_received.py` verbatim, with two changes: `_build_ctx` gains an `llm` parameter (defaulting to `_StubLLMClient()`) and a `tier0_table` parameter (defaulting to `None`), both forwarded into `DecideContext`.

```python
"""decide()-level tests for the Tier 0 fast path (spec §17).

Proves: a whitelist hit dispatches with caller_principal=regex_router
through the FULL gate/audit chain and never calls the LLM; a miss falls
through to the Tier 2 loop unchanged; a table entry pointing at a tool
regex_router may not call is refused by the Pre-action Gate (defense in
depth — bootstrap validation normally rejects such a table).
"""

from __future__ import annotations

# ... imports as in test_decision_utterance_received.py, plus:
from jarvis.decision.tier0 import load_tier0_table


class _ExplodingLLMClient(_StubLLMClient):
    """Chat must never be reached on a Tier 0 hit."""

    def chat(self, **kwargs: object) -> ChatResult:  # noqa: ARG002
        msg = "Tier 0 hit must not call the LLM"
        raise AssertionError(msg)


_TABLE_YAML = """\
- id: time_now
  pattern: "^现在几点了?[?？。.!！]?$"
  tool: get_current_time
  template: "现在是{spoken_time}。"
"""


def _table(tmp_path: Path) -> Tier0Table:
    p = tmp_path / "tier0_patterns.yaml"
    p.write_text(_TABLE_YAML, encoding="utf-8")
    return load_tier0_table(p)


def _emit_intent(conn: sqlite3.Connection, transcript: str) -> Event:
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": "T_t0", "channel": "cli_stdin"},
        correlation={"turn_id": "T_t0"},
    )


def test_tier0_hit_bypasses_llm_and_runs_full_audit_chain(tmp_path: Path) -> None:
    ctx, conn = _build_ctx(tmp_path, llm=_ExplodingLLMClient(), tier0_table=_table(tmp_path))
    try:
        result = decide(_emit_intent(conn, "现在几点"), ctx)
        assert result.response_plan is not None
        assert result.response_plan.text.startswith("现在是")
        types = [e.type for e in result.events_emitted]
        assert "turn.started" in types
        assert "action.proposed" in types
        assert "action.authorized" in types
        assert "turn.ended" in types
        proposed = next(e for e in result.events_emitted if e.type == "action.proposed")
        assert proposed.payload["caller_principal"] == "regex_router"
        assert proposed.payload["routed_by"] == "tier_0"
        assert proposed.payload["pattern_id"] == "time_now"
        gate_rows = [
            e for e in result.events_emitted
            if e.type == "gate.evaluated" and e.payload.get("gate") == "pre_action"
        ]
        assert gate_rows and gate_rows[0].payload["outcome"] == "pass"
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='action.result_observed'",
        ).fetchone()
        assert int(row[0]) == 1
        assert result.attention_channel == "queue_review"
    finally:
        conn.close()


def test_tier0_miss_falls_through_to_llm(tmp_path: Path) -> None:
    stub = _StubLLMClient()
    ctx, conn = _build_ctx(tmp_path, llm=stub, tier0_table=_table(tmp_path))
    try:
        result = decide(_emit_intent(conn, "帮我看看这个项目的进度"), ctx)
        assert result.response_plan is not None
        assert stub.chat_calls == 1
    finally:
        conn.close()


def test_tier0_none_table_behaves_like_day1(tmp_path: Path) -> None:
    stub = _StubLLMClient()
    ctx, conn = _build_ctx(tmp_path, llm=stub, tier0_table=None)
    try:
        decide(_emit_intent(conn, "现在几点"), ctx)
        assert stub.chat_calls == 1  # scaffold behavior preserved
    finally:
        conn.close()


def test_tier0_gate_refuses_disallowed_tool(tmp_path: Path) -> None:
    # Hand-build a table bypassing validate_tier0_table: spawn_worker
    # does NOT allow regex_router, so the Pre-action Gate must refuse.
    bad_yaml = (
        '- id: bad\n  pattern: "^现在几点$"\n  tool: spawn_worker\n  template: "x"\n'
    )
    p = tmp_path / "bad.yaml"
    p.write_text(bad_yaml, encoding="utf-8")
    ctx, conn = _build_ctx(
        tmp_path, llm=_ExplodingLLMClient(), tier0_table=load_tier0_table(p),
    )
    try:
        result = decide(_emit_intent(conn, "现在几点"), ctx)
        assert result.response_plan is not None
        assert "未执行" in result.response_plan.text
        gate_rows = [
            e for e in result.events_emitted
            if e.type == "gate.evaluated" and e.payload.get("gate") == "pre_action"
        ]
        assert gate_rows and gate_rows[0].payload["outcome"] == "refuse"
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='action.result_observed'",
        ).fetchone()
        assert int(row[0]) == 0  # never dispatched
    finally:
        conn.close()
```

(Write the real file with full imports and the copied harness — the fragments above define every assertion the implementation must satisfy.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit/test_decide_tier0.py -v`
Expected: FAIL — `DecideContext` has no `tier0_table` field / `tier_0_match` returns None.

- [ ] **Step 3: Implement**

3a. `jarvis/decision/intent.py` — replace the scaffold (keep the function exported; update the module docstring's Day-1 paragraph to say Tier 0 is table-driven now):

```python
from jarvis.decision.tier0 import Tier0Hit, Tier0Table, match_tier0


def tier_0_match(
    packet: SituationPacket,
    table: Tier0Table | None = None,
) -> Tier0Hit | None:
    """Spec §17 Tier 0: deterministic whitelist match on the transcript.

    Returns None when no table is loaded (Day-1 scaffold behavior),
    when the trigger is not a user utterance, or on a whitelist miss —
    the caller falls through to the Tier 2 LLM loop.
    """
    if not table:
        return None
    trigger = packet.trigger_event
    if trigger.type not in ("surface.user_intent", "utterance.received"):
        return None
    transcript = trigger.payload.get("transcript", "")
    if not isinstance(transcript, str):
        return None
    return match_tier0(transcript, table)
```

3b. `jarvis/decision/__init__.py`:

- `DecideContext`: append field `tier0_table: Tier0Table | None = None` (after `max_tool_iterations`; import `Tier0Table` under `TYPE_CHECKING` — plus runtime imports `Tier0Hit`, `render_tier0_response` from `jarvis.decision.tier0` for `_run_tier0_path`).
- `_handle_utterance`: replace the discarded call

```python
    # Tier 0 deterministic shortcut (spec §17): hit → dispatch through
    # the full gate/audit chain with caller_principal=regex_router,
    # LLM never invoked. Miss / no table → Tier 2 loop.
    hit = tier_0_match(packet, ctx.tier0_table)
    if hit is not None:
        return _run_tier0_path(hit, packet, policy, ctx, scratch)

    return _run_tool_use_loop(packet, policy, ctx, scratch)
```

- New `_run_tier0_path` (place directly after `_run_tool_use_loop`):

```python
_TIER0_GATE_REFUSED_TEXT = "这条指令被 Pre-action Gate 拦下，未执行。"


def _run_tier0_path(
    hit: Tier0Hit,
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Dispatch one Tier 0 whitelist hit (spec §17) without the LLM.

    Mirrors `_dispatch_one_tool_call` steps 2-7b for a sync, entity-free
    tool with caller_principal=REGEX_ROUTER: proposed → Pre-action Gate
    → authorized → L4 dispatch → Result Interpreter → deterministic
    template text → `_finalize_response` (Pre-emit Gate + attention).
    Spec §3.5.2: "低延迟路径，但仍要过 entity / policy / risk gate".
    """
    tool_def = _find_tool_def(ctx.tool_registry, hit.tool_name)
    if tool_def is None or tool_def.is_async:
        LOGGER.warning(
            "tier0: pattern %r targets unusable tool %r — falling back to LLM",
            hit.pattern_id,
            hit.tool_name,
        )
        return _run_tool_use_loop(packet, policy, ctx, scratch)

    action_id = _new_action_id()
    action_request = ActionRequest(
        action_id=action_id,
        tool_name=hit.tool_name,
        target_entity_ref=None,
        caller_principal=CallerPrincipal.REGEX_ROUTER,
        risk_level=tool_def.risk_level,
        arguments=dict(hit.tool_args),
        authorization_lease=None,
        run_id=None,
        turn_id=scratch.turn_id,
    )
    proposed_event = emit_event(
        ctx.conn,
        type="action.proposed",
        payload={
            "action_id": action_id,
            "tool_name": hit.tool_name,
            "caller_principal": CallerPrincipal.REGEX_ROUTER.value,
            "risk_level": tool_def.risk_level,
            "target_entity_ref": None,
            "turn_id": scratch.turn_id,
            "arguments": dict(hit.tool_args),
            "routed_by": "tier_0",
            "pattern_id": hit.pattern_id,
        },
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(proposed_event)

    gate = pre_action_gate(action_request, policy, packet.task_ledger_snapshot)
    gate_event = emit_event(
        ctx.conn,
        type="gate.evaluated",
        payload={
            "gate": "pre_action",
            "outcome": gate.outcome,
            "reasons": list(gate.reasons),
            "check_results": dict(gate.check_results),
            "action_id": action_id,
        },
        source_event_id=proposed_event.event_uid,
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(gate_event)
    if gate.outcome != "pass":
        return _finalize_response(_TIER0_GATE_REFUSED_TEXT, packet, ctx, scratch)

    authorized_event = emit_event(
        ctx.conn,
        type="action.authorized",
        payload={"action_id": action_id},
        source_event_id=gate_event.event_uid,
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(authorized_event)
    ctx.lifecycle.register(action_id)
    ctx.lifecycle.transition(action_id, "authorized")

    bundle = ctx.tool_registry.dispatch(
        action_request, ctx.conn, ctx.runtime_paths, ctx.lifecycle,
    )
    result_observed_uid = _latest_event_uid_of_type(
        ctx.conn, event_type="action.result_observed",
    )
    source_event_for_interpreter = result_observed_uid or proposed_event.event_uid
    for slot in bundle.slots:
        interpreted_events = result_interpreter(
            slot,
            source_event_id=source_event_for_interpreter,
            action_request=action_request,
            conn=ctx.conn,
            subject_ref_override=None,
        )
        scratch.events.extend(interpreted_events)

    primary_slot = bundle.slots[0]
    if primary_slot.error is not None:
        draft = f"这条指令执行出错（{primary_slot.error}），未产生结果。"
    else:
        draft = render_tier0_response(hit, primary_slot.payload)
    return _finalize_response(draft, packet, ctx, scratch)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit/test_decide_tier0.py tests/unit/test_intent.py tests/unit/test_decision_utterance_received.py -v`
Expected: all PASS (including the untouched neighbors — `tier_0_match(packet)` single-arg calls still work via the default).

- [ ] **Step 5: Run the full unit suite + gates**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit -q` then ruff/mypy strict on the three modified files + lint-imports.
Expected: all PASS / clean / KEPT, wall < 30s.

- [ ] **Step 6: Commit**

```bash
git add jarvis/decision/intent.py jarvis/decision/__init__.py tests/unit/test_decide_tier0.py
git commit -m "feat(decision): wire Tier 0 hit path into decide()

Batch 1 of the spec §17 Tier 0 regex fast path.

- jarvis/decision/intent.py — tier_0_match now table-driven
  (packet + Tier0Table), scaffold behavior preserved when no table.
- jarvis/decision/__init__.py — DecideContext.tier0_table field;
  _run_tier0_path: proposed → pre-action gate → authorized → dispatch
  → result interpreter → template draft → _finalize_response, with
  caller_principal=regex_router and routed_by/pattern_id audit keys.
- tests/unit/test_decide_tier0.py — LLM-bypass proof (exploding stub),
  full event-chain assertions, miss fall-through, gate-refusal defense
  when a table targets a disallowed tool.

Tier 1: lint-imports KEPT · ruff clean · mypy strict clean · unit
tests pass · wall <t>s (< 30s budget)."
```

---

### Task 4: bootstrap loading + shipped pattern file

**Files:**
- Modify: `jarvis/runtime/__init__.py` (`JarvisRuntime` ~line 128; `bootstrap_runtime_app` ~line 220; `drive_turn`'s `DecideContext(...)` ~line 564)
- Create: `config/tier0_patterns.yaml`
- Test: `tests/unit/test_runtime_tier0_bootstrap.py`

**Interfaces:**
- Consumes: Task 1's `load_tier0_table` / `validate_tier0_table` / `Tier0ConfigError` / `Tier0Table`; Task 3's `DecideContext.tier0_table`; existing `RuntimeBootstrapError`, `build_default_registry`, `CallerPrincipal`.
- Produces: `JarvisRuntime.tier0_table: Tier0Table` (default `()`); daemon + one-shot CLI both get Tier 0 for free (both go through `bootstrap_runtime_app` → `drive_turn`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_runtime_tier0_bootstrap.py`. Build a tmp config dir: copy the repo's real `config/jarvis.yaml` next to a test-authored `tier0_patterns.yaml` (mirror how `tests/unit/test_drive_turn.py` resolves `_CONFIG_PATH`); pass `config_path=tmp_config / "jarvis.yaml"`, `runtime_root=tmp_path / "rt"`.

```python
def test_bootstrap_loads_tier0_table(tmp_path: Path) -> None:
    cfg = _make_config_dir(tmp_path, tier0_yaml=_VALID_TWO_PATTERNS)
    rt = bootstrap_runtime_app(config_path=cfg / "jarvis.yaml", runtime_root=tmp_path / "rt")
    try:
        assert [p.pattern_id for p in rt.tier0_table] == ["time_now", "date_today"]
    finally:
        rt.conn.close()


def test_bootstrap_missing_tier0_file_disables_tier0(tmp_path: Path) -> None:
    cfg = _make_config_dir(tmp_path, tier0_yaml=None)
    rt = bootstrap_runtime_app(config_path=cfg / "jarvis.yaml", runtime_root=tmp_path / "rt")
    try:
        assert rt.tier0_table == ()
    finally:
        rt.conn.close()


def test_bootstrap_rejects_invalid_tier0_tool(tmp_path: Path) -> None:
    bad = '- id: bad\n  pattern: "^x$"\n  tool: spawn_worker\n  template: "t"\n'
    cfg = _make_config_dir(tmp_path, tier0_yaml=bad)
    with pytest.raises(RuntimeBootstrapError, match="spawn_worker"):
        bootstrap_runtime_app(config_path=cfg / "jarvis.yaml", runtime_root=tmp_path / "rt")
```

(`_make_config_dir` copies `config/jarvis.yaml` from the repo root — same `_REPO_ROOT` walk as `test_drive_turn.py` — and writes `tier0_patterns.yaml` when `tier0_yaml` is not None.)

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — `JarvisRuntime` has no `tier0_table` attribute.

- [ ] **Step 3: Implement**

3a. `jarvis/runtime/__init__.py` imports: `from jarvis.decision.tier0 import Tier0ConfigError, Tier0Table, load_tier0_table, validate_tier0_table`.

3b. `JarvisRuntime`: add field `tier0_table: Tier0Table = ()` (documented: "Spec §17 Tier 0 whitelist loaded from config/tier0_patterns.yaml; empty tuple = Tier 0 disabled.").

3c. `bootstrap_runtime_app`, after step 3 (registry + lifecycle):

```python
    # 3b. Spec §17 Tier 0 whitelist — sits next to jarvis.yaml so Allen
    #     edits one config directory. Invalid content fails the boot
    #     loudly (no silent pattern drops); missing file = Tier 0 off.
    tier0_path = config_path.parent / "tier0_patterns.yaml"
    try:
        tier0_table = load_tier0_table(tier0_path)
        regex_router_tools = registry.for_caller(CallerPrincipal.REGEX_ROUTER)
        validate_tier0_table(
            tier0_table,
            allowed_tool_names=frozenset(t.name for t in regex_router_tools),
            async_tool_names=frozenset(
                t.name for t in regex_router_tools if t.is_async
            ),
        )
    except Tier0ConfigError as exc:
        msg = f"runtime: {tier0_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc
```

…and add `tier0_table=tier0_table` to the `JarvisRuntime(...)` construction. (`CallerPrincipal` — check the module's existing imports; add to the `jarvis.shared` import line if absent.)

3d. `drive_turn`'s `DecideContext(...)` (~line 564): add `tier0_table=runtime.tier0_table`.

3e. Create `config/tier0_patterns.yaml`:

```yaml
# Tier 0 closed regex whitelist (spec §17). Loaded at daemon/CLI boot;
# restart `python -m jarvis serve` after editing.
#
# Entry schema:
#   id:       unique slug (shows up in action.proposed.pattern_id)
#   pattern:  full-sentence anchored regex — MUST start with ^ and end with $
#   tool:     a tool the regex_router principal may call (validated at boot)
#   args:     optional str->str map; "$N" pulls regex capture group N
#   template: response text; {name} fills from tool payload keys + args.
#             NEVER use completion-class words (完成/已完成/done/verified) —
#             the Pre-emit Gate downgrades them.
- id: time_now
  pattern: "^现在几点了?[?？。.!！]?$"
  tool: get_current_time
  template: "现在是{spoken_time}。"
- id: date_today
  pattern: "^今天(?:几号|星期几|周几)[?？。.!！]?$"
  tool: get_current_time
  template: "今天是{spoken_date}。"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest tests/unit/test_runtime_tier0_bootstrap.py tests/unit/test_drive_turn.py -v`
Expected: all PASS.

- [ ] **Step 5: Full Tier 1 gate run**

Run: full `pytest tests/unit -q` + ruff (all touched files) + mypy strict + lint-imports. Record wall time.
Expected: all green, wall < 30s.

- [ ] **Step 6: Commit**

```bash
git add jarvis/runtime/__init__.py config/tier0_patterns.yaml tests/unit/test_runtime_tier0_bootstrap.py
git commit -m "feat(runtime,deployment): load tier0_patterns.yaml at bootstrap

Batch 1 of the spec §17 Tier 0 regex fast path — final wiring step.

- jarvis/runtime/__init__.py — JarvisRuntime.tier0_table; bootstrap
  loads config/tier0_patterns.yaml, validates against the registry's
  regex_router surface, converts Tier0ConfigError into
  RuntimeBootstrapError; drive_turn passes the table to DecideContext.
- config/tier0_patterns.yaml — shipped whitelist: time_now +
  date_today → get_current_time; schema documented in-file for
  hand edits.
- tests/unit/test_runtime_tier0_bootstrap.py — load / absent-file /
  invalid-tool boot behaviors.

Tier 1: lint-imports KEPT · ruff clean · mypy strict clean · unit
tests pass · wall <t>s (< 30s budget)."
```

---

### Task 5: live smoke through the daemon + inherent panel

**Files:** none modified — verification only.

**Interfaces:** consumes the running system end-to-end.

- [ ] **Step 1: Restart the daemon from the worktree**

Kill the running `python -m jarvis serve` (started earlier from the main checkout), then from the worktree root: `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m jarvis serve` (background). Confirm `{"status":"ok"}` from `http://127.0.0.1:8006/api/health` and that the inherent frontend WS reconnects (lsof shows ESTABLISHED from InherentC).

- [ ] **Step 2: Smoke a Tier 0 hit**

`curl -s -X POST http://127.0.0.1:8006/inherent/submit` with the endpoint's JSON body (check `jarvis/surface/inherent_server.py` for the exact field name — the submit handler mints the turn and emits `surface.user_intent`), transcript `现在几点`. Expected: response text `现在是…点…分。` arrives on the WS/panel; event log shows `action.proposed` with `routed_by=tier_0` and NO `cost.recorded` for the turn (LLM never ran).

- [ ] **Step 3: Smoke a Tier 0 miss**

Submit `随便聊聊今天过得怎么样`. Expected: normal Tier 2 LLM answer; `cost.recorded` present.

- [ ] **Step 4: Report**

Summarize both event traces (sqlite query on `~/.jarvis/mac_events.db` or the runtime root in use) to Allen for review. NO further commits; Allen reviews the branch before merge.

---

## Self-Review Notes

- Spec coverage: §17 hit-path semantics (Task 3), `^...$` anchoring (Task 1 loader), §3.5.2 gate discipline (Task 3 `_run_tier0_path` + refusal test), §14.5 caller scoping (Task 2 `allowed_callers` + Task 4 boot validation), §3.4.12 Pre-emit (deterministic drafts routed through `_finalize_response`). Deliberately out of scope (Allen-approved): list_tasks/create_task patterns (spec §14.3 gray zones), weather, timers, smart home.
- Type consistency: `Tier0Hit`/`Tier0Table` names used identically in Tasks 1/3/4; `tier0_table` field name identical on `DecideContext` and `JarvisRuntime`; payload keys `spoken_time`/`spoken_date` produced in Task 2 and consumed by the templates shipped in Task 4.
- Known judgment calls implementers must not "fix": templates deliberately avoid completion keywords; `_run_tier0_path` deliberately re-uses `_finalize_response` (LLM retry branch is unreachable for keyword-free drafts); gate-refusal path deliberately returns fixed text instead of falling through to the LLM.
