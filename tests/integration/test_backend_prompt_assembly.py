"""The backend request after the 2026-09-21 prompt round: what the model receives.

Observables: the rendered blocks from memory.db and the ``messages`` /
``system`` handed to the LLM client for one turn.

- history starts at ``session.history_since``; earlier rows stay in the
  store (the ``since=""`` render still shows them, so the cutoff is what
  removes them, not their absence);
- history replays one message per record, the role read off the row's
  source, adjacent rows of one role joined into one message;
- an answer written with the retired ``<voice>``/``<document>`` envelope
  renders as its words;
- the profile is its own block, for the system prompt;
- the per-turn state rides at the head of this turn's user message under
  one header, so a request carries the history and exactly one more user
  message, and no ``[system context]`` note; a history ending on an
  unanswered user row folds into that message.
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
    from collections.abc import Iterator, Sequence
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
    # An answer that copied the old history label into its own text.
    (
        "new-3",
        "2026-09-15T02:50:12-07:00",
        "jarvis_live",
        "[2026-09-15T02:50:10-07:00] jarvis: 记得带伞。",
    ),
    ("new-4", "2026-09-15T02:51:00-07:00", "allen", "好"),
    ("new-5", "2026-09-15T02:51:03-07:00", "allen", "还有呢"),
    ("new-6", "2026-09-21T15:00:00-07:00", "allen", "谢谢"),
)

HISTORY = (
    {"role": "user", "content": "[2026-09-15T02:50:02-07:00] allen: 明天天气怎么样"},
    {"role": "assistant", "content": "[2026-09-15T02:50:09-07:00] jarvis: 明天多云。"},
)
STATUS = (
    "[当前状态｜程序提供，不是用户说的话]\n"  # noqa: RUF001 — Chinese punctuation is intentional.
    "时间：2026-09-21T15:37-07:00 周一\n"  # noqa: RUF001 — Chinese punctuation is intentional.
    "交互方式：语音\n"  # noqa: RUF001 — Chinese punctuation is intentional.
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


def _flat(history: Sequence[dict[str, str]]) -> str:
    return "\n".join(turn["content"] for turn in history)


def test_history_replays_records_by_role_from_since(tmp_path: Path) -> None:
    """Rows before the cutoff stay out; roles follow the source; same-role rows join."""
    db = _memory_db(tmp_path)

    ctx = render_context(db, exclude_id="new-6", since=SINCE, now=NOW)

    assert ctx.profile == "[关于 Allen]\n- 用户叫 Allen。\n- 默认用中文。"
    # ADR 0044: words only, no [ts] source: label; one date line opens a day.
    assert ctx.history == (
        {"role": "user", "content": "[9月15日 周二]\n明天天气怎么样"},
        {"role": "assistant", "content": "明天多云。\n最高 18 度。\n记得带伞。"},
        {"role": "user", "content": "好\n还有呢"},
    )
    assert ctx.now == "时间：2026-09-21T15:37-07:00 周一 · 距上次交流 6 天 12 小时"  # noqa: RUF001 — Chinese punctuation is intentional.

    # The null surface: without the cutoff the same store shows the old rows,
    # and the next day's first user row gets its own date line.
    unbounded = render_context(db, exclude_id="new-6", since="", now=NOW).history
    assert unbounded[0]["content"] == "[9月12日 周六]\n1001夜赶一下"
    assert unbounded[2]["content"] == "[9月15日 周二]\n明天天气怎么样"
    assert "<voice>" not in _flat(unbounded)


def test_empty_store_renders_empty_profile_and_no_history(tmp_path: Path) -> None:
    """An empty profile is no block at all; an empty history is no message at all."""
    db = tmp_path / "memory.db"
    open_memory_db(db).close()

    ctx = render_context(db, exclude_id="", since=SINCE, now=NOW)

    assert ctx.profile == ""
    assert ctx.history == ()
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


def _drive_one_turn(
    tmp_path: Path, history: Sequence[dict[str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    """Run one ``surface.user_intent`` turn; return the ``system`` and ``messages`` sent."""
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
        history=history,
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
    return llm.requests[0]


def test_turn_request_is_history_by_role_then_one_user_message_with_status(
    tmp_path: Path,
) -> None:
    """One turn hands the model the history as turns, then one user message with the state."""
    system, messages = _drive_one_turn(tmp_path, HISTORY)

    assert system.endswith("[关于 Allen]\n- 用户叫 Allen。")
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[:2] == list(HISTORY)
    assert messages[2]["content"] == f"{STATUS}\n后天呢"
    assert not any("[system context]" in message["content"] for message in messages)


def test_history_ending_on_a_user_row_folds_into_this_turn(tmp_path: Path) -> None:
    """An unanswered user row joins this turn's message ahead of the state header."""
    unanswered = {"role": "user", "content": "[2026-09-21T15:30:00-07:00] allen: 还有呢"}

    _, messages = _drive_one_turn(tmp_path, (*HISTORY, unanswered))

    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"] == f"{unanswered['content']}\n\n{STATUS}\n后天呢"
