"""Regression: ``decide()`` must dispatch ``utterance.received`` like ``surface.user_intent``.

ADR-0005 §5.1 "Important wiring detail" — the ``_user_intent_watcher``
folds BOTH ``surface.user_intent`` (keyboard) AND ``utterance.received``
(voice ASR) into one cursor and calls :func:`drive_turn` on both. The
voice surface emits ``utterance.received`` (not ``surface.user_intent``)
because the ASR pipeline owns the audit + normalization step, so
``decide()`` must accept the voice trigger as a valid turn-driving
event. Without the dispatch widening, every voice utterance no-ops in
``decide`` and the watcher times out 5 s later with ``turn.failed`` —
no AI response, no TTS, the user sees only "No Speech".

This test asserts the routing at the ``decide()`` boundary: the
``utterance.received`` trigger MUST emit ``turn.started`` (the
``_handle_utterance`` branch) and a non-``None`` ``response_plan``,
proving it took the same code path as ``surface.user_intent``.
"""
from __future__ import annotations

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
    """RuntimePathsLike-shaped stub — utterance branch never reads it directly."""

    event_log: Path
    artifacts_root: Path

    def artifact_dir_for_run(self, run_id: str) -> Path:
        """Return a per-run dir under ``artifacts_root`` (created on demand)."""
        out = self.artifacts_root / run_id
        out.mkdir(parents=True, exist_ok=True)
        return out


class _StubLLMClient:
    """Stand-in :class:`LLMClient` that returns canned limitation-language text.

    The canned text passes the Pre-emit Gate at attempt 0 (no completion
    keywords), so ``decide()`` finalizes in one chat call without a retry
    or network access.
    """

    _CANNED_TEXT = "agent reported, status unverified — awaiting your reply."

    def __init__(self) -> None:
        self.chat_calls = 0
        self.model = "stub-model"
        self._last_input_tokens: int | None = 0
        self._last_output_tokens: int | None = 0
        self._last_finish_reason: str | None = "stop"

    @property
    def last_input_tokens(self) -> int | None:
        return self._last_input_tokens

    @property
    def last_output_tokens(self) -> int | None:
        return self._last_output_tokens

    @property
    def last_finish_reason(self) -> str | None:
        return self._last_finish_reason

    @contextmanager
    def fresh_context(self) -> Iterator[_StubLLMClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],  # noqa: ARG002 — stub ignores args.
        system: str,  # noqa: ARG002 — stub ignores args.
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 — stub ignores args.
        tool_choice: str | None = "auto",  # noqa: ARG002 — stub ignores args.
    ) -> ChatResult:
        self.chat_calls += 1
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
    """Build a DecideContext + return the open Event Log conn."""
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    llm = _StubLLMClient()
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", lifecycle),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
    )
    return ctx, conn


def _count_rows(conn: sqlite3.Connection, event_type: str) -> int:
    cursor = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,),
    )
    row = cursor.fetchone()
    return int(row[0])


def test_decide_routes_utterance_received_to_handle_utterance(tmp_path: Path) -> None:
    """``decide(utterance.received)`` MUST take the surface.user_intent branch.

    Regression: the post-ADR-0005 smoke run logged
    ``decide(): unknown trigger type 'utterance.received' — no-op`` and
    timed out 5s later with ``turn.failed``, leaving the inherent card
    with the user's transcript echo (Bug 2a fix) but no AI reply. With
    the dispatch widening, decide() runs ``_handle_utterance`` and emits
    ``turn.started`` + a non-None ``response_plan``.
    """
    ctx, conn = _build_ctx(tmp_path)
    try:
        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={
                "transcript": "你好",
                "turn_id": "T_voice_test",
                "channel": "inherent_ptt",
                "language": "zh-CN",
                "confidence": 0.9,
            },
            correlation={"turn_id": "T_voice_test"},
        )

        result = decide(trigger, ctx)

        # The handler must have run (turn.started emitted) — the unknown-trigger
        # no-op path emits zero events.
        assert _count_rows(conn, "turn.started") == 1, (
            "decide(utterance.received) did not emit turn.started — likely "
            "still hitting the 'unknown trigger type' no-op branch."
        )
        # And it must have produced a response_plan (the no-op returns None).
        assert result.response_plan is not None, (
            "decide(utterance.received) returned response_plan=None; expected "
            "the surface.user_intent branch to finalize with a ResponsePlan."
        )
        assert cast("_StubLLMClient", ctx.llm_client).chat_calls == 1, (
            "decide(utterance.received) must enter the tool-use loop exactly "
            "once for this single-turn happy path."
        )
    finally:
        conn.close()
