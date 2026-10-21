"""A scripted summariser and the records and sweep ticks it is tested with."""

from __future__ import annotations

import asyncio
import re
from contextlib import closing
from datetime import timedelta
from typing import TYPE_CHECKING

from jarvis.decision.compaction import REQUIRED_HEADINGS
from jarvis.state.memory_db import local_now, open_memory_db
from tests.support.model import ScriptedModel

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime.session_compaction import CompactionSweep
    from tests.support.model import Request, Step

HEADINGS = "\n".join(f"{heading}\nnone" for heading in REQUIRED_HEADINGS)


def fold_summary(request: Request) -> str:
    """One summary: the headings and the numbers of the first and last record this call folded."""
    said = sentence_numbers(str(request.messages[0]["content"]).split("[Records")[1])
    return f"## Conversation summary\n{HEADINGS}\nfolds records {said[0]} to {said[-1]}"


def compact_summariser(*steps: Step) -> ScriptedModel:
    """The compact preset's model: a well-formed summary for each request past ``steps``."""
    return ScriptedModel(*steps, otherwise=fold_summary, model="fake-summariser")


def fold_prompts(model: ScriptedModel) -> list[str]:
    """What each request carried: the previous summary, then the records to fold in."""
    return [str(request.messages[0]["content"]) for request in model.requests]


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
