"""Drive one turn and return the system prompt and messages the model was sent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import _MISHEARD, DecideContext, decide
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.model import ScriptedModel
from tests.support.runtime_paths import StubRuntimePaths

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from jarvis.decision import LifecycleLike, RuntimePathsLike, ToolRegistryLike

HISTORY = (
    {"role": "user", "content": "[2026-09-15T02:50:02-07:00] allen: 明天天气怎么样"},
    {"role": "assistant", "content": "[2026-09-15T02:50:09-07:00] jarvis: 明天多云。"},
)


STATUS = (
    "[Current state | from the program, not the user's words]\n"
    "Time: 2026-09-21T15:37-07:00 Monday\n"
    "This turn: spoken; your answer is read aloud.\n"
    f"{_MISHEARD}\n"
)


REPLY_LINE = "[Reply language for this turn: Chinese]"


def drive_one_turn(
    tmp_path: Path,
    history: Sequence[dict[str, str]],
    connected_apps: str | None = None,
    live_context: tuple[str, ...] = (),
    time_note: str = "Time: 2026-09-21T15:37-07:00 Monday",
) -> tuple[str, list[dict[str, Any]]]:
    """Run one ``surface.user_intent`` turn; return the ``system`` and ``messages`` sent."""
    conn = open_event_log(tmp_path / "events.db")
    paths = StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    llm = ScriptedModel("明天多云。")
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", build_default_registry()),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=llm.as_llm_client(),
        system_prompt="stub system prompt\n\n[About the user]\n- 用户叫 Allen。",
        history=history,
        time_note=time_note,
        connected_apps=connected_apps,
        live_context=live_context,
    )
    try:
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "后天呢", "turn_id": "T_prompt_001", "channel": "inherent_ptt"},
            correlation={"turn_id": "T_prompt_001"},
        )
        result = decide(trigger, ctx)
    finally:
        conn.close()
    assert result.response_plan is not None
    assert result.response_plan.text == "明天多云。"
    (request,) = llm.requests
    return request.system, request.messages
