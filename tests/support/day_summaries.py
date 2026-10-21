"""Day-summary records, a scripted summariser and the days they are timed on."""

from __future__ import annotations

import re
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from jarvis.decision.day_summary import REQUIRED_HEADINGS
from jarvis.decision.llm import ChatResult
from jarvis.runtime.day_summary import VERBATIM_UNDER_CHARS
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from pathlib import Path

TODAY = datetime.now().astimezone().date()


GOOD = "\n".join(f"{heading}\nnone" for heading in REQUIRED_HEADINGS)


def day_ago(ago: int) -> date:
    return TODAY - timedelta(days=ago)


def record_id(ago: int, index: int) -> str:
    return f"{ago:016x}{index:016x}"  # 32 hex digits: the shape of an event uid


def stamp_ago(ago: int, hour: int, minute: int = 0, *, utc: bool = False) -> str:
    moment = datetime.combine(day_ago(ago), time(hour, minute)).astimezone()
    return (moment.astimezone(UTC) if utc else moment).isoformat(timespec="seconds")


def insert_records(path: Path, rows: list[tuple[str, str, str, str]], *, pad: bool = False) -> None:
    """``pad`` lengthens every record past the verbatim threshold, so its day is summarised."""
    if pad:
        rows = [(rid, ts, src, text + "长" * VERBATIM_UNDER_CHARS) for rid, ts, src, text in rows]
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany("INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)", rows)


class DaySummariser:
    """Stands in for the day-summary preset's client; ``replies`` maps a day to its answer."""

    provider = "openai"
    model = "fake-day-summariser"

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.replies: dict[str, str] = {}
        self.systems: list[str] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        **_kwargs: object,
    ) -> ChatResult:
        text = str(messages[0]["content"])
        day = re.match(r"\[Records of (\S+),", text).group(1)  # type: ignore[union-attr]
        self.asked.append(day)
        self.systems.append(system)
        return ChatResult(
            text=self.replies.get(day, f"## {day}\n{GOOD}"),
            tool_calls=(),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            raw={},
            model_used=self.model,
        )
