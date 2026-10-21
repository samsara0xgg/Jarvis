"""A DecideContext over stubs, to drive one decide() with a canned draft."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import DecideContext
from jarvis.decision.llm import ChatResult
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import open_event_log
from tests.support.runtime_paths import StubRuntimePaths

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.decision import LifecycleLike, RuntimePathsLike, ToolRegistryLike
    from jarvis.decision.llm import LLMClient


class _StubLLMClient:
    """Returns a parametrized draft. One chat per turn; never raises.

    The draft intentionally carries the ``完成`` completion keyword to
    prove that the gate is no longer downgrading on keyword presence
    alone when no subject is in scope.
    """

    native_tool_search = False  # Jarvis's own tool_search (ADR 0287)

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


def build_decide_context(
    tmp_path: Path,
    *,
    draft_text: str,
) -> tuple[DecideContext, sqlite3.Connection, _StubLLMClient]:
    conn = open_event_log(tmp_path / "events.db")
    paths = StubRuntimePaths(
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
