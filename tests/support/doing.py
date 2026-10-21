"""The stores of one day and the writer for the doing-run tests."""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.llm import ChatResult
from jarvis.runtime.doing_run import DoingSettings
from jarvis.state import picture_notes as note_records
from jarvis.state import places
from jarvis.state.attachments import AttachmentRef
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.decision.llm import LLMClient

VANCOUVER = ZoneInfo("America/Vancouver")


DAY = date(2026, 10, 7)


def doing_at(hour: int, minute: int = 0, *, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), VANCOUVER)


def doing_ms(moment: datetime) -> int:
    return int(moment.timestamp()) * 1000


@dataclass
class World:
    """The stores of one day, and the sources the day is folded from."""

    memory: Path
    log: Path
    sources: DaySources


def visit_row(  # noqa: PLR0913 — a visit is these six things
    log: Path,
    uid: str,
    arrived: datetime,
    departed: datetime | None,
    place: str,
    lat: float = 1.0,
) -> None:
    payload: dict[str, Any] = {
        "lat": lat,
        "lng": 2.0,
        "accuracy_m": 30.0,
        "arrived_at_ms": doing_ms(arrived),
        "place": place,
    }
    if departed is not None:
        payload["departed_at_ms"] = doing_ms(departed)
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn,
            type="phone.visit_observed",
            payload=payload,
            ts_epoch_ms=doing_ms(arrived),
            event_uid=uid,
            ingestion_node="phone",
        )


def say_words(memory: Path, rid: str, moment: datetime, source: str, text: str) -> None:
    with closing(open_memory_db(memory)) as conn, conn:
        conn.execute(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            (rid, moment.isoformat(timespec="seconds"), source, text),
        )


def screen_row(log: Path, moment: datetime, now: str | None, *activities: str) -> None:
    item = {
        "now": None if now is None else {"text": now, "basis": "observed", "refs": []},
        "activities": [{"text": text} for text in activities],
    }
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn,
            type="work_state.revised",
            ts_epoch_ms=doing_ms(moment),
            payload={"trigger": "test", "item": item},
        )


def picture_row(world: World, name: str, moment: datetime, note: str) -> None:
    with closing(open_event_log(world.log)) as conn:
        emit_event(
            conn,
            type="surface.user_intent",
            ts_epoch_ms=doing_ms(moment),
            correlation={"turn_id": f"T-{name}"},
            payload={"transcript": "x", "turn_id": f"T-{name}", "attachments": [name]},
        )
    ref = AttachmentRef(name, "image", "IMG_1.png", 1, "image/png")
    note_records.append(world.memory, ref, note)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """10-07: the library 08:00-11:30, a cafe 12:00-13:00, the library 13:30-14:45; it is 15:00.

    He said what he was doing at 09:05, the work state saw his screen at 10:00, and a picture of
    noodles came at 12:20.
    """
    memory, log = tmp_path / "memory.db", tmp_path / "events.db"
    open_memory_db(memory).close()
    open_event_log(log).close()
    places.save(memory, name="UVic Library", lat=1.0, lng=2.0, role="school", origin="user")
    visit_row(log, "v1", doing_at(8), doing_at(11, 30), "UVic Library")
    visit_row(log, "v2", doing_at(12), doing_at(13), "Corner Cafe", lat=1.01)
    visit_row(log, "v3", doing_at(13, 30), doing_at(14, 45), "UVic Library")
    say_words(memory, "r1", doing_at(9, 5), "allen", "我在图书馆写论文")
    say_words(memory, "r2", doing_at(9, 6), "jarvis", "好的，加油")
    screen_row(log, doing_at(10), "Writing the thesis in Overleaf")  # what he is doing lately
    world = World(memory, log, DaySources(VANCOUVER, None, memory))
    picture_row(world, "noodles", doing_at(12, 20), "A bowl of noodles on a wooden table")
    return world


class Writer:
    """Stands in for the doing preset's client: ``reply`` maps the prompt to its answer."""

    provider = "anthropic"
    model = "fake-haiku"

    def __init__(self, reply: Callable[[str], str]) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.asked: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], system: str, **_kwargs: object) -> ChatResult:
        prompt = str(messages[0]["content"])
        self.prompts.append(prompt)
        self.asked.append(messages)
        self.systems.append(system)
        return ChatResult(
            text=self.reply(prompt),
            tool_calls=(),
            finish_reason="stop",
            input_tokens=100,
            output_tokens=50,
            raw={},
            model_used=self.model,
            tokens_in=100,
            tokens_out=50,
        )


def client(writer: Writer) -> LLMClient:
    """The fake as the preset's client: the run only has the cost recorder call its ``chat``."""
    return cast("LLMClient", writer)


def tile(prompt: str) -> str:
    """An answer for any day: one span per segment, said plainly."""
    spans: list[dict[str, Any]] = [
        {
            "from": one[1],
            "to": one[2],
            "kind": "doing",
            "title": "Work on it",
            "detail": None,
            "category": "work",
            "basis": "inferred",
            "confidence": "low",
            "clues": [],
            "question": None,
        }
        for one in re.finditer(r"^(t\d+)-(t\d+) \|", prompt, re.MULTILINE)
    ]
    return json.dumps({"spans": spans})


SETTINGS = DoingSettings(preset="p", prompt="write what he was doing")


CLOCKED = replace(SETTINGS, clock_drafts=True)
