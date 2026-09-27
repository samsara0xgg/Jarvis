"""ADR 0061 — Allen's words switch a conversation's thinking on; response.started says so.

Six turns of one evening, driven through ``drive_turn`` with the model scripted: the on-word turns
thinking on for its own sentence and the next, an off-word turns it off, and a pause of more
than ten minutes ends it. The observables are ``response.started.reasoning_effort`` and, after
each turn, the ``on`` the companion reads from ``GET /inherent/think`` (ADR 0064).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

import jarvis.decision.think_mode as think_mode_module
import jarvis.runtime as runtime_module
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.think_mode import load_think_mode
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, Wave1FeatureFlags, _wave4_response_flags, drive_turn
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log
from tests.canary._helpers import repo_root
from tests.integration.test_wave4a_response_run import _final_result, _realtime_config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_PRESET = {"model": "gpt-5.6-luna", "base_url": "https://example.invalid", "max_tokens": 64}
_LLM: dict[str, Any] = {
    "provider": "openai",
    "default_preset": "luna",
    # The shipped words, read as YAML the way the daemon reads them.
    "think": yaml.safe_load((repo_root() / "config/jarvis.yaml").read_text())["llm"]["think"],
    "presets": {
        "luna": {**_PRESET, "reasoning_effort": "none"},
        "luna-think": {**_PRESET, "reasoning_effort": "medium", "api": "responses"},
    },
}
_MINUTE = 60_000
# (minutes after the first turn, what Allen says, the effort its answer must use)
_EVENING = (
    (0, "今天天气怎么样", "none"),
    (1, "想想吧 这周先做哪个项目", "medium"),
    (2, "那第二个呢", "medium"),
    (3, "不用想了 几点了", "none"),
    (4, "好好想想晚饭吃什么", "medium"),
    (16, "你好", "none"),
)


@dataclass
class _Clock:
    """The runtime's ``time`` module with a settable wall clock."""

    now_ms: int

    def time(self) -> float:
        return self.now_ms / 1000

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — every other time function is real.
        return getattr(time, name)


def test_on_word_thinks_until_off_word_or_a_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each answer's response.started names the effort the conversation called for."""
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    config = _realtime_config(lifecycle=True, cancel=False)
    runtime = JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=LLMClient(_LLM),
        system_prompt="",
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True, lifecycle_terminal_cas=True
        ),
        response_flags=_wave4_response_flags(config),
        llm_session_factory=LLMSessionFactory(_LLM),
        committed_event_bus=CommittedEventBus(),
        think_mode=load_think_mode(_LLM),
    )
    monkeypatch.setattr(runtime_module, "decide", _final_result())
    start = int(time.time() * 1000)
    clock = _Clock(start)
    monkeypatch.setattr(runtime_module, "time", clock)
    monkeypatch.setattr(think_mode_module, "time", clock)
    think = runtime.think_mode
    assert think is not None
    shown: list[bool] = []

    for minute, said, _ in _EVENING:
        clock.now_ms = start + minute * _MINUTE
        turn_id = f"T{minute}"
        intent = emit_event(
            conn,
            type="surface.user_intent",
            payload={
                "transcript": said,
                "turn_id": turn_id,
                "channel": "inherent_wake",
                "language": "zh-CN",
            },
            correlation={"turn_id": turn_id},
            ts_epoch_ms=clock.now_ms,
        )
        drive_turn(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        shown.append(think.status(conn)["on"])

    efforts = [
        json.loads(p)["reasoning_effort"]
        for (p,) in conn.execute(
            "SELECT payload_json FROM events WHERE type='response.started' ORDER BY id"
        )
    ]
    assert efforts == [effort for _, _, effort in _EVENING]
    assert shown == [effort == "medium" for _, _, effort in _EVENING]
    conn.close()
