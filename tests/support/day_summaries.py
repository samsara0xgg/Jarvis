"""Day-summary records, a scripted summariser and the days they are timed on."""

from __future__ import annotations

import re
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from jarvis.decision.day_summary import REQUIRED_HEADINGS
from jarvis.runtime.day_summary import VERBATIM_UNDER_CHARS
from jarvis.shared.clock import system_zone, zone_key
from jarvis.state.memory_db import open_memory_db
from tests.support.model import Failure, Reply, ScriptedModel

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from tests.support.model import Request

TODAY = datetime.now().astimezone().date()
LOCAL_ZONE = ZoneInfo(zone_key(system_zone()) or "UTC")
"""The machine's zone, which the records' stamps are written in."""


GOOD = "\n".join(f"{heading}\nnone" for heading in REQUIRED_HEADINGS)


def today_at(hour: int, minute: int = 0, *, days: int = 0) -> datetime:
    """``hour``:``minute`` on :data:`TODAY` (``days`` later) in the machine's zone."""
    return datetime.combine(TODAY + timedelta(days=days), time(hour, minute), LOCAL_ZONE)


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


def day_of(request: Request) -> str:
    """The day a request is about: its first message opens ``[Records of <day>, ...``."""
    match = re.search(r"\[Records of (\S+),", str(request.messages[0]["content"]))
    if match is None:
        msg = "the request does not carry a day's records"
        raise AssertionError(msg)
    return match.group(1)


def day_summariser(replies: Mapping[str, str | Exception] | None = None) -> ScriptedModel:
    """The day-summary preset's model: each day gets its entry in ``replies``, else a valid one.

    ``replies`` is read as each request arrives, so a test may change it between runs; an
    exception as the entry is the request failing with it.
    """
    answers: Mapping[str, str | Exception] = {} if replies is None else replies

    def respond(request: Request) -> Reply | Failure:
        day = day_of(request)
        answer = answers.get(day, f"## {day}\n{GOOD}")
        return Failure(answer) if isinstance(answer, Exception) else Reply(answer)

    return ScriptedModel(otherwise=respond, model="fake-day-summariser")


def asked_days(model: ScriptedModel) -> list[str]:
    """The day each request was about, in the order they were sent."""
    return [day_of(request) for request in model.requests]
