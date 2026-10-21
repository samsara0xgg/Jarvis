"""A scripted summariser and the records and sweep ticks it is tested with."""

from __future__ import annotations

import asyncio
import re
from contextlib import closing
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from jarvis.decision.compaction import REQUIRED_HEADINGS
from jarvis.decision.llm import ChatResult
from jarvis.state.memory_db import local_now, open_memory_db

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime.session_compaction import CompactionSweep

HEADINGS = "\n".join(f"{heading}\nnone" for heading in REQUIRED_HEADINGS)


class CompactSummariser:
    """Stands in for the compact preset's client: records each request it is sent."""

    provider = "openai"
    model = "fake-summariser"

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.fail_next = False
        self.reply: str | None = None

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: object) -> ChatResult:
        """One summary: the headings and the number of records this call folded."""
        if self.fail_next:
            self.fail_next = False
            msg = "provider unreachable"
            raise TimeoutError(msg)
        text = str(messages[0]["content"])
        self.requests.append(text)
        said = sentence_numbers(text.split("[Records")[1])
        text = self.reply or (
            f"## Conversation summary\n{HEADINGS}\nfolds records {said[0]} to {said[-1]}"
        )
        return ChatResult(
            text=text,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            raw={},
            model_used=self.model,
        )


def fill_records(path: Path, first: int, last: int) -> None:
    """Records ``first`` to ``last`` inclusive, one minute apart, ending at the newest."""
    base = local_now() - timedelta(minutes=500)
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            [
                (
                    f"r{index:04d}",
                    (base + timedelta(minutes=index)).isoformat(timespec="seconds"),
                    "allen" if index % 2 == 0 else "jarvis",
                    f"第{index}句",
                )
                for index in range(first, last + 1)
            ],
        )


def rolling_summaries(path: Path) -> list[tuple[str, str]]:
    with closing(open_memory_db(path)) as conn:
        return [
            (str(upto), str(text))
            for upto, text in conn.execute(
                "SELECT upto_record_id, summary FROM summaries ORDER BY rowid",
            )
        ]


def sentence_numbers(text: str) -> list[int]:
    return [int(number) for number in re.findall(r"第(\d+)句", text)]


def tick_sweep(sweep: CompactionSweep) -> None:
    """One supervisor tick, and wait for the job it starts, if any."""

    async def _go() -> None:
        sweep.tick()
        if sweep._task is not None:  # noqa: SLF001
            await sweep._task  # noqa: SLF001

    asyncio.run(_go())
