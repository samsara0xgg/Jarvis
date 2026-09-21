"""The backend request after the 2026-09-21 prompt round: what the model receives.

Observables: the rendered blocks from memory.db and the ``messages`` /
``system`` handed to the LLM client for one turn.

- history starts at ``session.history_since``; earlier rows stay in the
  store (the ``since=""`` render still shows them, so the cutoff is what
  removes them, not their absence);
- an answer written with the retired ``<voice>``/``<document>`` envelope
  renders as its words;
- the profile is its own block, for the system prompt;
- the per-turn state rides at the head of this turn's user message under
  one header, so a request carries the history and exactly one more user
  message, and no ``[system context]`` note.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import DecideContext, decide
from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import open_memory_db, render_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.decision import LifecycleLike, RuntimePathsLike, ToolRegistryLike

SINCE = "2026-09-14T00:00:00-07:00"
NOW = datetime.fromisoformat("2026-09-21T15:37:00-07:00")

ROWS = (
    ("old-1", "2026-09-12T07:18:51-07:00", "allen", "1001夜赶一下"),
    (
        "old-2",
        "2026-09-12T07:18:55-07:00",
        "jarvis",
        "<voice>\n旧的语音回答\n</voice>\n<document>\n旧文档\n</document>",
    ),
    ("new-1", "2026-09-15T02:50:02-07:00", "allen", "明天天气怎么样"),
    (
        "new-2",
        "2026-09-15T02:50:09-07:00",
        "jarvis",
        "<voice>\n明天多云。\n</voice>\n<document>\n最高 18 度。\n</document>",
    ),
    ("new-3", "2026-09-21T15:00:00-07:00", "allen", "谢谢"),
)


def _memory_db(tmp_path: Path) -> Path:
    path = tmp_path / "memory.db"
    with open_memory_db(path) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)", ROWS,
        )
        conn.executemany(
            "INSERT INTO profile (id, ts, text) VALUES (?, ?, ?)",
            [("p1", SINCE, "用户叫 Allen。"), ("p2", SINCE, "默认用中文。")],
        )
    return path


def test_history_starts_at_since_and_shows_words_not_envelopes(tmp_path: Path) -> None:
    """Rows before the cutoff stay out; envelope tags render as words."""
    db = _memory_db(tmp_path)

    ctx = render_context(db, exclude_id="new-3", since=SINCE, now=NOW)

    assert ctx.profile == "[关于 Allen]\n- 用户叫 Allen。\n- 默认用中文。"
    assert ctx.history.startswith(
        "[对话记录, 全文, 时间正序]\n[2026-09-15T02:50:02-07:00] allen: 明天天气怎么样\n",
    )
    assert "1001夜" not in ctx.history, "a record before history_since reached the prompt"
    assert "<voice>" not in ctx.history
    assert "</document>" not in ctx.history
    assert "[2026-09-15T02:50:09-07:00] jarvis: 明天多云。\n最高 18 度。" in ctx.history
    assert "谢谢" not in ctx.history, "the current turn's own utterance is excluded"
    assert ctx.now == "时间：2026-09-21T15:37-07:00 周一 · 距上次交流 6 天 12 小时"  # noqa: RUF001 — Chinese punctuation is intentional.

    # The null surface: without the cutoff the same store shows the old rows.
    assert "1001夜" in render_context(db, exclude_id="new-3", since="", now=NOW).history


def test_empty_store_renders_empty_profile_and_no_records(tmp_path: Path) -> None:
    """An empty profile is no block at all, not a placeholder line."""
    db = tmp_path / "memory.db"
    open_memory_db(db).close()

    ctx = render_context(db, exclude_id="", since=SINCE, now=NOW)

    assert ctx.profile == ""
    assert ctx.history == "[对话记录, 全文, 时间正序]\n(无)"
    assert ctx.now == "时间：2026-09-21T15:37-07:00 周一"  # noqa: RUF001 — Chinese punctuation is intentional.


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path


class _CapturingLLMClient:
    """Answers with fixed text and keeps the request it was handed."""

    def __init__(self) -> None:
        self.model = "stub-model"
        self.requests: list[tuple[str, list[dict[str, Any]]]] = []

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
    def fresh_context(self) -> Iterator[_CapturingLLMClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.requests.append((system, [dict(message) for message in messages]))
        return ChatResult(
            text="明天多云。",
            tool_calls=(),
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def test_turn_request_is_history_then_one_user_message_with_status(tmp_path: Path) -> None:
    """One turn hands the model the history, then one user message carrying the state."""
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    llm = _CapturingLLMClient()
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", build_default_registry()),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt\n\n[关于 Allen]\n- 用户叫 Allen。",
        memory_note="[对话记录, 全文, 时间正序]\n[2026-09-15T02:50:02-07:00] allen: 明天天气怎么样",
        time_note="时间：2026-09-21T15:37-07:00 周一",  # noqa: RUF001 — Chinese punctuation is intentional.
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
    assert len(llm.requests) == 1
    system, messages = llm.requests[0]
    assert system.endswith("[关于 Allen]\n- 用户叫 Allen。")
    assert [message["role"] for message in messages] == ["user", "user"]
    assert messages[0]["content"] == ctx.memory_note
    assert messages[1]["content"] == (
        "[当前状态｜程序提供，不是用户说的话]\n"  # noqa: RUF001 — Chinese punctuation is intentional.
        "时间：2026-09-21T15:37-07:00 周一\n"  # noqa: RUF001 — Chinese punctuation is intentional.
        "交互方式：语音\n"  # noqa: RUF001 — Chinese punctuation is intentional.
        "\n"
        "后天呢"
    )
    assert not any("[system context]" in message["content"] for message in messages)
