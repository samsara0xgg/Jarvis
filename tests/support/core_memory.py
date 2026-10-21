"""A scripted core-memory consolidator and the day-summary schedule that runs it."""

from __future__ import annotations

import json
import re
from datetime import time
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm import ChatResult
from jarvis.runtime.core_memory import CoreMemorySettings
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import MemorySettings
from tests.support.day_summaries import DaySummariser, insert_records, record_id, stamp_ago

if TYPE_CHECKING:
    from pathlib import Path


class Consolidator:
    """Stands in for the core_memory preset's client; ``script`` maps a day to its answers."""

    provider = "openai"
    model = "fake-consolidator"

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.inputs: dict[str, str] = {}
        self.script: dict[str, list[str | Exception]] = {}

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: object) -> ChatResult:
        text = str(messages[0]["content"])
        day = re.search(r"\[Records of (\S+),", text).group(1)  # type: ignore[union-attr]
        self.asked.append(day)
        self.inputs[day] = text
        answers = self.script.get(day) or ['{"changes": []}']
        answer = answers[min(self.asked.count(day) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return ChatResult(
            text=answer,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            raw={},
            model_used=self.model,
        )


def consolidator_changes(*ops: dict[str, Any]) -> str:
    return json.dumps({"changes": list(ops)}, ensure_ascii=False)


def core_schedule(tmp_path: Path, model: Consolidator, **core: Any) -> DaySummarySchedule:  # noqa: ANN401
    log = tmp_path / "events.db"
    open_event_log(log).close()
    schedule = DaySummarySchedule(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=DaySummarySettings(at=time(5, 0), preset="p", prompt="summarise", max_chars=500),
        llm_config={},
        event_log_path=log,
        pricing_table={},
        core_memory=CoreMemorySettings(preset="c", prompt="consolidate", **core),
    )
    schedule._client = DaySummariser()  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    schedule._core_client = model  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    return schedule


def seed_days(path: Path, *ago: int) -> None:
    """One short user record on each of the given days (kept word for word: no summary call)."""
    insert_records(
        path,
        [(record_id(n, 1), stamp_ago(n, 9), "allen", f"第{n}天说的话") for n in ago],
    )
