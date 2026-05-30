"""E2E regression: conversational turn must not be force-downgraded.

Per design doc 2026-05-27-pre-emit-gate-optimal-design.md §7 acceptance
criteria:

  1. Conversational pass-through — a turn with no active task subject
     surfaces the LLM's own draft text (not the canonical "找不到对应的
     task" hard-refusal template).
  2. Gate event audit — exactly one ``gate.evaluated`` event with
     ``attempt=0``, ``permission=allow_completion_language``,
     ``downgrade_required=False``, ``active_claim_levels=[]``.
  6. No new LLM calls — ``cost.recorded`` count for the turn drops to
     one (the original draft generation); the attempt-1 retry and
     attempt-2 template branch must not fire.

Spec basis: §3.4.12 v0 (only gate consequential claims), §3.4.4
(LLMSituationPacket ``active_task?`` is optional, so empty-task turns
are first-class).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path

    def artifact_dir_for_run(self, run_id: str) -> Path:
        out = self.artifacts_root / run_id
        out.mkdir(parents=True, exist_ok=True)
        return out


class _StubLLMClient:
    """Returns a parametrized draft. One chat per turn; never raises.

    The draft intentionally carries the ``完成`` completion keyword to
    prove that the gate is no longer downgrading on keyword presence
    alone when no subject is in scope.
    """

    def __init__(self, draft_text: str) -> None:
        self._draft_text = draft_text
        self.chat_calls = 0
        self.model = "stub-model"

    @property
    def last_input_tokens(self) -> int | None:
        return 0

    @property
    def last_output_tokens(self) -> int | None:
        return 0

    @property
    def last_finish_reason(self) -> str | None:
        return "stop"

    @contextmanager
    def fresh_context(self) -> Iterator[_StubLLMClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],  # noqa: ARG002
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.chat_calls += 1
        return ChatResult(
            text=self._draft_text,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _build_ctx(
    tmp_path: Path,
    *,
    draft_text: str,
) -> tuple[DecideContext, sqlite3.Connection, _StubLLMClient]:
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    llm = _StubLLMClient(draft_text=draft_text)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", lifecycle),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
    )
    return ctx, conn, llm


def _gate_evaluated_payloads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'gate.evaluated' "
        "ORDER BY ts_epoch_ms"
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _count_rows(conn: sqlite3.Connection, event_type: str) -> int:
    cursor = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,),
    )
    return int(cursor.fetchone()[0])


def test_conversational_turn_passes_through_without_downgrade(
    tmp_path: Path,
) -> None:
    """`介绍一下你自己` against empty ledger → LLM draft surfaces unmodified."""
    # The draft contains the "完成" keyword. Pre-change this triggers
    # force_limitation → retry → template → hard_refusal. Post-change
    # the None-subject pass-through ships it verbatim.
    draft = "我是 Jarvis，我可以帮你完成各种任务。"  # noqa: RUF001 — intentional CJK fullwidth punctuation; the draft mirrors a real LLM Chinese reply.
    ctx, conn, llm = _build_ctx(tmp_path, draft_text=draft)
    try:
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={
                "transcript": "介绍一下你自己",
                "turn_id": "T_conv_001",
            },
            correlation={"turn_id": "T_conv_001"},
        )

        result = decide(trigger, ctx)

        # Acceptance criterion 1: the LLM draft surfaces verbatim.
        assert result.response_plan is not None
        assert result.response_plan.text == draft, (
            "Conversational turn was downgraded — pre-emit gate fired despite no "
            f"subject. Got: {result.response_plan.text!r}"
        )
        assert "找不到对应的 task" not in result.response_plan.text, (
            "Conversational turn surfaced the F1 / hard-refusal canonical text — "
            "indicates the gate retry chain ran to exhaustion."
        )
        assert result.response_plan.permission == "allow_completion_language"
        assert result.response_plan.downgrade_required is False

        # Acceptance criterion 2: exactly one gate.evaluated, attempt 0,
        # outcome=allow_completion_language, claim_levels=[]. The gate
        # event payload key is "outcome" (not "permission") and
        # downgrade_required is a ResponsePlan field — its absence from
        # the gate payload is by design; outcome=allow_completion_language
        # already conveys non-downgrading.
        gates = _gate_evaluated_payloads(conn)
        assert len(gates) == 1, (
            f"Expected exactly 1 gate.evaluated event, got {len(gates)}: "
            f"the retry chain (attempts 1 and 2) must not fire when no "
            f"subject is in scope. Payloads: {gates!r}"
        )
        verdict = gates[0]
        assert verdict["gate"] == "pre_emit"
        assert verdict["attempt"] == 0
        assert verdict["outcome"] == "allow_completion_language"
        assert verdict["claim_levels"] == []

        # Acceptance criterion 6: exactly one cost.recorded — the original
        # draft generation. No retry round-trip.
        cost_count = _count_rows(conn, "cost.recorded")
        assert cost_count == 1, (
            f"Expected exactly 1 cost.recorded event, got {cost_count}: "
            f"the attempt-1 LLM retry must not fire on the no-subject path."
        )
        assert llm.chat_calls == 1, (
            f"LLM stub recorded {llm.chat_calls} chat calls; expected 1."
        )
    finally:
        conn.close()
