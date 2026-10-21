"""A DecideContext over stubs, to drive one decide() with a canned draft."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from jarvis.decision import DecideContext
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import open_event_log
from tests.support.model import ScriptedModel
from tests.support.runtime_paths import StubRuntimePaths

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.decision import LifecycleLike, RuntimePathsLike, ToolRegistryLike


def build_decide_context(
    tmp_path: Path,
    *,
    draft_text: str,
) -> tuple[DecideContext, sqlite3.Connection, ScriptedModel]:
    """The context, its log and the model that answers every request with ``draft_text``.

    A draft may carry the ``完成`` completion keyword, to show the gate does not downgrade on
    the keyword alone when no subject is in scope.
    """
    conn = open_event_log(tmp_path / "events.db")
    paths = StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    llm = ScriptedModel(otherwise=draft_text)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", lifecycle),
        llm_client=llm.as_llm_client(),
        system_prompt="stub system prompt",
    )
    return ctx, conn, llm
