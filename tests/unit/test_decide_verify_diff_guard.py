"""Tier-1 wiring: ``decide()`` refuses a redundant verify_diff via Pre-action Gate.

Companion to ``test_verify_diff_idempotence.py`` (which pins the pure detection
helper). This drives the real ``decide()`` tool-use loop with a scripted,
LLM-free stub to prove the *wiring*: when the untrusted Tier-2 LLM proposes a
``verify_diff`` for a task that was already verify-proposed this turn, the
decision loop overrides the Pre-action Gate to ``refuse`` (reason
``redundant_verify_diff_this_turn``) and the verify_diff is never dispatched —
so it cannot emit a second ``observation`` slot or a duplicate ``task.verified``.

A live burn cannot prove this deterministically (the LLM only re-proposes
verify_diff non-deterministically — the 2026-05-30 L5 burn that surfaced the bug
double-proposed; a later burn proposed once). Seeding the "first" verify_diff
``action.proposed`` row lets this test force the redundant second proposal every
run, with no Codex and no git (the guard refuses before dispatch).
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
from jarvis.decision.llm import ChatResult, LLMClient, ToolCall
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

_TURN = "T_guard_test"


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path

    def artifact_dir_for_run(self, run_id: str) -> Path:
        out = self.artifacts_root / run_id
        out.mkdir(parents=True, exist_ok=True)
        return out


class _ScriptedLLM:
    """Proposes ``verify_diff`` on the first chat call, then settles with text.

    The verify_diff proposal targets the single seeded open task; on the second
    chat call (after the gate refuses the redundant proposal) it returns plain
    limitation-language text so the Pre-emit Gate passes at attempt 0 and the
    loop finalizes without network.
    """

    _CANNED_TEXT = "agent reported, status unverified — awaiting your reply."

    def __init__(self) -> None:
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
    def fresh_context(self) -> Iterator[_ScriptedLLM]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],  # noqa: ARG002 — stub ignores history.
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.chat_calls += 1
        if self.chat_calls == 1:
            return ChatResult(
                text="",
                tool_calls=(
                    ToolCall(
                        call_id="call_verify_1",
                        name="verify_diff",
                        arguments_json='{"task_id": "task_X"}',
                    ),
                ),
                finish_reason="tool_calls",
                input_tokens=0,
                output_tokens=0,
                raw={},
                model_used="stub-model",
                tokens_in=0,
                tokens_out=0,
            )
        return ChatResult(
            text=self._CANNED_TEXT,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _build_ctx(tmp_path: Path) -> tuple[DecideContext, sqlite3.Connection]:
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", build_default_registry()),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", _ScriptedLLM()),
        system_prompt="stub system prompt",
    )
    return ctx, conn


def _rows(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    cursor = conn.execute(
        "SELECT payload_json FROM events WHERE type = ? ORDER BY id", (event_type,),
    )
    return [json.loads(r[0]) for r in cursor.fetchall()]


def test_decide_refuses_redundant_verify_diff_this_turn(tmp_path: Path) -> None:
    """A second verify_diff for an already-verify-proposed task this turn is refused.

    Seeds the "first" verify_diff ``action.proposed`` for (task_X, turn) plus the
    open task, then drives ``decide()`` (turn-correlated to the same turn) with a
    stub LLM that proposes verify_diff once more. The guard must override the
    Pre-action Gate to refuse and the verify_diff must never dispatch.
    """
    ctx, conn = _build_ctx(tmp_path)
    try:
        # Open task so the resolver maps "task_X" -> task_X (single candidate).
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "demo task", "source": "manual"},
        )
        # The "first" verify_diff already proposed THIS turn (simulated).
        emit_event(
            conn,
            type="action.proposed",
            payload={
                "action_id": "A_first_verify",
                "tool_name": "verify_diff",
                "caller_principal": "jarvis_llm",
                "risk_level": "L0",
                "target_entity_ref": "task_X",
                "turn_id": _TURN,
                "arguments": {},
            },
            correlation={"turn_id": _TURN},
        )

        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={
                "transcript": "审核一下昨天那个 task",
                "turn_id": _TURN,
                "channel": "inherent_ptt",
                "language": "zh-CN",
                "confidence": 0.9,
            },
            correlation={"turn_id": _TURN},
        )

        result = decide(trigger, ctx)

        # The redundant verify_diff must have been refused by the Pre-action Gate.
        pre_action_refusals = [
            g
            for g in _rows(conn, "gate.evaluated")
            if g.get("gate") == "pre_action"
            and g.get("outcome") == "refuse"
            and "redundant_verify_diff_this_turn" in g.get("reasons", [])
        ]
        assert len(pre_action_refusals) == 1, (
            "expected exactly one redundant_verify_diff Pre-action Gate refusal; "
            f"gate events={_rows(conn, 'gate.evaluated')}"
        )

        # Refused before dispatch → no verify_diff result_observed this turn.
        assert _rows(conn, "action.result_observed") == [], (
            "refused verify_diff must not dispatch — no action.result_observed expected"
        )
        # And no task.verified could have been (re-)emitted off a refused verify.
        assert _rows(conn, "task.verified") == []

        # The loop still settled into a response (the LLM got the refusal and
        # produced text instead of re-verifying).
        assert result.response_plan is not None
        assert cast("_ScriptedLLM", ctx.llm_client).chat_calls == 2
    finally:
        conn.close()
