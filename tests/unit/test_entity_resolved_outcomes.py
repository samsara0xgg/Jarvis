"""Unit tests for :func:`jarvis.decision._emit_entity_resolved` outcome ladder.

Per ADR § Resolver contract — the ``entity.resolved.outcome`` ladder is
``{"resolved", "ambiguous", "not_found"}``:

- ``"resolved"`` — ``ResolverResult.confidence in {"exact", "high"}`` OR
  ``confidence="fuzzy"`` with non-None ``resolved_to``.
- ``"ambiguous"`` — ``confidence="fuzzy"`` with multi-candidate +
  ``resolved_to=None``.
- ``"not_found"`` — ``confidence="none"`` (formerly ``"failed"``;
  B-NEW-4 renamed for consistency with the audit-trail vocabulary).

The tests construct a minimal :class:`DecideContext` with a real SQLite
Event Log connection, drive ``_emit_entity_resolved`` directly with
synthetic / real :class:`ResolverResult` values, and assert the emitted
``entity.resolved`` event payload.

LLM-free (no LLM stub needed — the helper is a pure event emitter).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    _emit_entity_resolved,
    _resolver_outcome,
    decide,
)
from jarvis.decision.resolver import (
    ResolverResult,
    resolve_task_ref,
    resolve_task_ref_by_window,
)
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.shared import Event
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.projections import TaskLedger, TaskLedgerSnapshot

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.decision.llm import LLMClient


# --- stubs ----------------------------------------------------------------


@dataclass(frozen=True)
class _StubRuntimePaths:
    """RuntimePathsLike-shaped stub — never read by ``_emit_entity_resolved``."""

    event_log: Path
    artifacts_root: Path

    def artifact_dir_for_run(self, run_id: str) -> Path:
        """Return a per-run dir under ``artifacts_root`` (created on demand)."""
        out = self.artifacts_root / run_id
        out.mkdir(parents=True, exist_ok=True)
        return out


class _StubLLMClient:
    """Stand-in for :class:`jarvis.decision.llm.LLMClient`.

    ``_emit_entity_resolved`` never calls ``chat``; this stub exists
    only to satisfy the frozen :class:`DecideContext` dataclass shape.
    """

    model = "stub-model"


def _build_ctx(tmp_path: Path) -> DecideContext:
    """Build a DecideContext with a real Event Log conn (test owns cleanup)."""
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    return DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", lifecycle),
        llm_client=cast("LLMClient", _StubLLMClient()),
        system_prompt="stub system prompt",
    )


def _seed_trigger(ctx: DecideContext, *, transcript: str = "go") -> Event:
    """Emit a surface.user_intent so we have a real source_event_id."""
    return emit_event(
        ctx.conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": "T-test"},
        correlation={"turn_id": "T-test"},
    )


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


def _snapshot_from(*events: Event) -> TaskLedgerSnapshot:
    return TaskLedger.from_events(events).snapshot()


# --- _resolver_outcome unit tests (pure mapping) ---------------------------


def test_resolver_outcome_not_found_for_zero_candidate() -> None:
    """``confidence='none'`` MUST map to ``'not_found'`` (B-NEW-4 rename)."""
    result = ResolverResult(
        resolved_to=None,
        confidence="none",
        candidates=(),
        match_basis="no open task matched",
    )
    assert _resolver_outcome(result) == "not_found"


def test_resolver_outcome_ambiguous_for_multi_candidate_fuzzy() -> None:
    """``confidence='fuzzy'`` + resolved_to=None → ``'ambiguous'``."""
    result = ResolverResult(
        resolved_to=None,
        confidence="fuzzy",
        candidates=("task_a", "task_b"),
        match_basis="multi-candidate ambiguous",
    )
    assert _resolver_outcome(result) == "ambiguous"


def test_resolver_outcome_resolved_for_exact_high_and_fuzzy_with_target() -> None:
    """``exact`` / ``high`` / ``fuzzy + resolved_to`` all → ``'resolved'``."""
    exact_result = ResolverResult(
        resolved_to="task_x",
        confidence="exact",
        candidates=("task_x",),
        match_basis="exact match",
    )
    assert _resolver_outcome(exact_result) == "resolved"

    high_result = ResolverResult(
        resolved_to="task_x",
        confidence="high",
        candidates=("task_x",),
        match_basis="single open task substring match",
    )
    assert _resolver_outcome(high_result) == "resolved"

    fuzzy_with_target = ResolverResult(
        resolved_to="task_x",
        confidence="fuzzy",
        candidates=("task_x",),
        match_basis="fuzzy single",
    )
    assert _resolver_outcome(fuzzy_with_target) == "resolved"


# --- _emit_entity_resolved payload tests -----------------------------------


def test_resolver_emits_entity_resolved_not_found(tmp_path: Path) -> None:
    """Empty ledger drives ``confidence='none'`` → payload outcome ``'not_found'``.

    Exercises the real :func:`resolve_task_ref` against an empty
    snapshot, then threads the result through ``_emit_entity_resolved``
    and asserts the emitted ``entity.resolved`` event payload carries
    ``outcome="not_found"`` with empty ``candidates``. This is the
    B-NEW-4 rename regression guard.
    """
    ctx = _build_ctx(tmp_path)
    try:
        trigger = _seed_trigger(ctx, transcript="昨天那个 task")
        snapshot = _snapshot_from()  # empty ledger
        result = resolve_task_ref("yesterday's task", snapshot)
        assert result.confidence == "none"
        assert result.candidates == ()

        event = _emit_entity_resolved(
            ctx,
            natural_ref="yesterday's task",
            result=result,
            turn_id=None,
            source_event_id=trigger.event_uid,
        )

        assert event.type == "entity.resolved"
        assert event.payload["outcome"] == "not_found"
        assert event.payload["candidates"] == []
        assert event.payload["resolved_to"] is None
        assert event.payload["confidence"] == "none"
        assert event.payload["entity_type"] == "task"
        assert event.payload["natural_ref"] == "yesterday's task"
        # No resolver_warning for confidence='none' (only fuzzy gets it).
        assert "resolver_warning" not in event.payload
    finally:
        ctx.conn.close()


def test_resolver_emits_entity_resolved_ambiguous(tmp_path: Path) -> None:
    """Multi-candidate ledger → payload ``outcome='ambiguous'`` with ≥2 candidates.

    Exercises the real :func:`resolve_task_ref` against a multi-task
    snapshot, then asserts the emitted ``entity.resolved`` event carries
    ``outcome="ambiguous"`` and ``len(candidates) >= 2``. Also asserts
    ``resolver_warning=True`` (the fuzzy-confidence marker the audit
    chain uses to flag low-quality matches).
    """
    ctx = _build_ctx(tmp_path)
    try:
        trigger = _seed_trigger(ctx, transcript="the codex task")
        snapshot = _snapshot_from(
            _task_created_event(
                task_id="task_old",
                goal="legacy refactor",
                event_uid="evt-old",
                ts_epoch_ms=1_000_000,
            ),
            _task_created_event(
                task_id="task_new",
                goal="codex refactor",
                event_uid="evt-new",
                ts_epoch_ms=2_000_000,
            ),
        )
        result = resolve_task_ref("the codex task", snapshot)
        assert result.confidence == "fuzzy"
        assert result.resolved_to is None
        assert len(result.candidates) >= 2

        event = _emit_entity_resolved(
            ctx,
            natural_ref="the codex task",
            result=result,
            turn_id=None,
            source_event_id=trigger.event_uid,
        )

        assert event.type == "entity.resolved"
        assert event.payload["outcome"] == "ambiguous"
        assert len(event.payload["candidates"]) >= 2
        assert event.payload["resolved_to"] is None
        assert event.payload["confidence"] == "fuzzy"
        # Fuzzy confidence MUST set the warning flag.
        assert event.payload["resolver_warning"] is True
    finally:
        ctx.conn.close()


def test_f1_short_circuit_emits_entity_resolved_not_found(tmp_path: Path) -> None:
    """F1 short-circuit MUST emit ``entity.resolved`` with ``outcome='not_found'``.

    Before B-NEW-4, the F1 deterministic short-circuit
    (:func:`_no_task_to_refer_to`) returned a hard-refusal plan without
    emitting an ``entity.resolved`` event — breaking the audit chain on
    demonstrative + empty-ledger queries.

    This test drives ``decide()`` with a demonstrative-task transcript
    against an empty ledger and asserts the result's emitted events
    contain a single ``entity.resolved`` event with ``outcome='not_found'``,
    ordered BEFORE ``turn.ended``.
    """
    ctx = _build_ctx(tmp_path)
    try:
        turn_id = "T-f1"
        trigger = emit_event(
            ctx.conn,
            type="surface.user_intent",
            payload={"transcript": "昨天那个 task 给 Codex 跑一下", "turn_id": turn_id},
            correlation={"turn_id": turn_id},
        )

        result = decide(trigger, ctx)

        # F1 short-circuit produced a hard-refusal plan.
        assert result.response_plan is not None
        assert "未验证" in result.response_plan.text

        entity_events = [
            e for e in result.events_emitted if e.type == "entity.resolved"
        ]
        assert len(entity_events) == 1, (
            "F1 short-circuit must emit exactly one entity.resolved event; "
            f"got {[e.type for e in result.events_emitted]!r}"
        )
        entity_event = entity_events[0]
        assert entity_event.payload["outcome"] == "not_found"
        assert entity_event.payload["candidates"] == []
        assert entity_event.payload["resolved_to"] is None
        assert entity_event.payload["entity_type"] == "task"
        # natural_ref carries the transcript so the audit chain shows
        # WHICH reference we tried to resolve.
        assert "昨天那个 task" in entity_event.payload["natural_ref"]

        # entity.resolved MUST be emitted before turn.ended.
        types = [e.type for e in result.events_emitted]
        if "turn.ended" in types:
            assert types.index("entity.resolved") < types.index("turn.ended"), (
                "entity.resolved must precede turn.ended in the F1 short-"
                f"circuit emission order; got {types!r}"
            )
    finally:
        ctx.conn.close()


def test_resolver_by_window_emits_entity_resolved_not_found(tmp_path: Path) -> None:
    """Window resolver with no in-window candidates → ``outcome='not_found'``.

    Covers the Day-2 ``resolve_task_ref_by_window`` path: even though
    the ledger has a single open task, it falls OUTSIDE the requested
    [since_ts, until_ts] window, so the resolver returns
    ``confidence='none'`` and the emitted payload carries
    ``outcome='not_found'`` (post-rename).
    """
    ctx = _build_ctx(tmp_path)
    try:
        trigger = _seed_trigger(ctx, transcript="yesterday's task")
        snapshot = _snapshot_from(
            _task_created_event(
                task_id="task_solo",
                goal="ship it",
                event_uid="evt-solo",
                ts_epoch_ms=5_000_000,
            ),
        )
        # Window that excludes the only task (created at ts=5_000_000).
        result = resolve_task_ref_by_window(
            "yesterday",
            snapshot,
            since_ts=1_000,
            until_ts=2_000,
        )
        assert result.confidence == "none"

        event = _emit_entity_resolved(
            ctx,
            natural_ref="yesterday",
            result=result,
            turn_id=None,
            source_event_id=trigger.event_uid,
        )

        assert event.payload["outcome"] == "not_found"
        assert event.payload["candidates"] == []
    finally:
        ctx.conn.close()
