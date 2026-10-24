"""A scripted core-memory consolidator and the day-summary schedule that runs it."""

from __future__ import annotations

import json
from datetime import time
from typing import TYPE_CHECKING, Any

from jarvis.runtime.core_memory import CoreMemorySettings
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import MemorySettings
from tests.support.clock import FakeClock
from tests.support.day_summaries import (
    day_of,
    day_summariser,
    insert_records,
    record_id,
    stamp_ago,
)
from tests.support.model import Failure, Reply, ScriptedModel

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.model import Request


def consolidator(script: dict[str, list[str | Exception]] | None = None) -> ScriptedModel:
    """The core_memory preset's model: ``script`` maps a day to the answers its requests get.

    A day's answers are used up in order and the last one repeats, so a list set anew starts
    over; an exception fails the request with it, and a day with no list gets no changes.
    """
    answers = {} if script is None else script

    def respond(request: Request) -> Reply | Failure:
        queue = answers.get(day_of(request)) or ['{"changes": []}']
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return Failure(answer) if isinstance(answer, Exception) else Reply(answer)

    return ScriptedModel(otherwise=respond, model="fake-consolidator")


def consolidation_input(model: ScriptedModel, day: str) -> str:
    """What the consolidator was last shown for ``day``: its first message."""
    for request in reversed(model.requests):
        if day_of(request) == day:
            return str(request.messages[0]["content"])
    msg = f"the consolidator was never asked about {day}"
    raise AssertionError(msg)


def consolidator_changes(*ops: dict[str, Any]) -> str:
    return json.dumps({"changes": list(ops)}, ensure_ascii=False)


def core_schedule(
    tmp_path: Path,
    model: ScriptedModel,
    *,
    summariser: ScriptedModel | None = None,
    rebuild_model: ScriptedModel | None = None,
    **core: Any,  # noqa: ANN401
) -> DaySummarySchedule:
    """The nightly schedule over a temp store: ``summariser`` writes days, ``model`` the core.

    ``rebuild_model`` answers the monthly rebuild (ADR 0389) when ``core`` carries ``rebuild``.
    """
    log = tmp_path / "events.db"
    open_event_log(log).close()
    return DaySummarySchedule(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=DaySummarySettings(at=time(5, 0), preset="p", prompt="summarise", max_chars=500),
        llm_config={},
        event_log_path=log,
        pricing_table={},
        clock=FakeClock(),
        core_memory=CoreMemorySettings(preset="c", prompt="consolidate", **core),
        client=(day_summariser() if summariser is None else summariser).as_llm_client(),
        core_client=model.as_llm_client(),
        rebuild_client=None if rebuild_model is None else rebuild_model.as_llm_client(),
    )


def seed_days(path: Path, *ago: int) -> None:
    """One short user record on each of the given days (kept word for word: no summary call)."""
    insert_records(
        path,
        [(record_id(n, 1), stamp_ago(n, 9), "allen", f"第{n}天说的话") for n in ago],
    )
