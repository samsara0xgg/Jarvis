"""Unit tests for ``jarvis.decision.resolver.resolve_task_ref`` (ADR § Resolver contract).

Covers:

- Single open task → confidence in {"exact", "high", "fuzzy"}, resolved_to
  set, candidates length 1, match_basis non-empty.
- Empty ledger → confidence="none", candidates=().
- Multi-candidate → confidence="fuzzy", candidates length > 1.
- Empty natural_ref → confidence="none" (defensive).
- Resolved task is always a member of the open-tasks set (canary H10
  contract: non-None resolved_to must be in ``ledger_snapshot.open_tasks()``).

LLM-free pure tests; no fixtures touch the LLM client.
"""

from __future__ import annotations

import ast
from pathlib import Path

from jarvis.decision.resolver import resolve_task_ref
from jarvis.shared import Event
from jarvis.state.projections import TaskLedger, TaskLedgerSnapshot


def _task_created_event(
    *,
    task_id: str,
    goal: str,
    event_uid: str,
    ts_epoch_ms: int = 1_000_000,
) -> Event:
    """Synthesize a `task.created` event for ledger seeding."""
    return Event(
        event_uid=event_uid,
        type="task.created",
        schema_version=1,
        ts_epoch_ms=ts_epoch_ms,
        payload={"task_id": task_id, "goal": goal},
        source_event_id=None,
        correlation=None,
    )


def _build_snapshot(*events: Event) -> TaskLedgerSnapshot:
    """Fold ``events`` into a TaskLedger and return its snapshot."""
    ledger = TaskLedger.from_events(events)
    return ledger.snapshot()


def test_resolve_single_open_task_high_confidence():
    """Substring match yields confidence='high' with single candidate."""
    snapshot = _build_snapshot(
        _task_created_event(
            task_id="task_X",
            goal="finish codex refactor",
            event_uid="evt-0001",
        ),
    )
    result = resolve_task_ref("codex", snapshot)

    assert result.resolved_to == "task_X"
    assert result.confidence == "high"
    assert result.candidates == ("task_X",)
    assert result.match_basis != ""


def test_resolve_single_open_task_fuzzy_when_no_substring():
    """Non-substring natural_ref still resolves under single-open-task rule."""
    snapshot = _build_snapshot(
        _task_created_event(
            task_id="task_X",
            goal="ship the spec doc",
            event_uid="evt-0002",
        ),
    )
    result = resolve_task_ref("yesterday's task", snapshot)

    # Single-open-task path NEVER returns confidence='none' per ADR contract.
    assert result.resolved_to == "task_X"
    assert result.confidence in ("exact", "high", "fuzzy")
    assert result.candidates == ("task_X",)
    assert result.match_basis


def test_resolve_empty_ledger_returns_none():
    """Empty ledger → confidence='none', empty candidates."""
    snapshot = _build_snapshot()
    result = resolve_task_ref("yesterday's task", snapshot)

    assert result.resolved_to is None
    assert result.confidence == "none"
    assert result.candidates == ()
    assert result.match_basis == "no open task matched"


def test_resolve_empty_natural_ref_returns_none():
    """Defensive: empty natural_ref → confidence='none'."""
    snapshot = _build_snapshot(
        _task_created_event(
            task_id="task_X",
            goal="ship",
            event_uid="evt-0003",
        ),
    )
    result = resolve_task_ref("", snapshot)

    assert result.resolved_to is None
    assert result.confidence == "none"
    assert result.candidates == ()


def test_resolve_multi_candidate_returns_fuzzy_none():
    """Two+ open tasks → confidence='fuzzy', resolved_to=None (caller confirms)."""
    snapshot = _build_snapshot(
        _task_created_event(
            task_id="task_old",
            goal="legacy refactor",
            event_uid="evt-0010",
            ts_epoch_ms=1_000_000,
        ),
        _task_created_event(
            task_id="task_new",
            goal="codex refactor",
            event_uid="evt-0011",
            ts_epoch_ms=2_000_000,
        ),
    )
    result = resolve_task_ref("the codex task", snapshot)

    assert result.resolved_to is None
    assert result.confidence == "fuzzy"
    assert len(result.candidates) > 1
    # Most-recent-first ranking — task_new should be at index 0.
    assert result.candidates[0] == "task_new"


def test_resolved_to_is_member_of_open_tasks_when_set():
    """ADR canary H10: non-None resolved_to MUST be in open_tasks()."""
    snapshot = _build_snapshot(
        _task_created_event(
            task_id="task_X",
            goal="ship",
            event_uid="evt-0020",
        ),
    )
    result = resolve_task_ref("any ref", snapshot)
    open_ids = {record.task_id for record in snapshot.open_tasks()}

    if result.resolved_to is not None:
        assert result.resolved_to in open_ids


def test_resolver_module_does_not_import_llm():
    """Canary H10: jarvis.decision.resolver MUST NOT import jarvis.decision.llm.

    Soft canary — full AST scan happens in Step 11. This early check
    catches the obvious case during Step 9 development.
    """
    src = Path("jarvis/decision/resolver.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "jarvis.decision.llm", (
                "resolver.py must not import jarvis.decision.llm per ADR § Resolver contract"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "jarvis.decision.llm", (
                    "resolver.py must not import jarvis.decision.llm"
                )
