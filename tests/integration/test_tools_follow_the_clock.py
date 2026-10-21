"""ADR 0320: a tool takes the time from the clock in its context, and from nothing else.

Each moved tool is called through the dispatcher on a fake clock set years from the wall clock,
in a zone that is not the machine's, with no time-travel patch: what it says about today, now or
when it was done is the fake clock's. A tool that still read the wall clock would answer for the
real today.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.archive_tool import build_archive_tool
from jarvis.execution.conversation_tool import build_conversation_tool
from jarvis.execution.tools import build_default_registry
from jarvis.shared import CallerPrincipal
from jarvis.state.memory_db import open_memory_db
from tests.support.clock import FakeClock
from tests.support.tool_context import tool_context
from tests.support.tool_world import ToolWorld, tool_chain, tool_request

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ZONE = ZoneInfo("Pacific/Auckland")
THEN = datetime(2031, 3, 2, 9, 0, tzinfo=ZONE)
"""A Sunday morning in Auckland, five years ahead of the suite and half a day from it."""


class World:
    """The default registry over a temp root and a clock at :data:`THEN`."""

    def __init__(self, root: Path) -> None:
        """Wire the tools to the clock."""
        self.clock = FakeClock(THEN, zone=ZONE)
        self.memory = root / "memory.db"
        with closing(open_memory_db(self.memory)):
            pass
        registry = build_default_registry(memory_db_path=self.memory, clock=self.clock)
        self.fx = ToolWorld(
            root / "run", tools=tuple(registry.get_definitions()), clock=self.clock,
        )
        self.n = 0

    def call(
        self, name: str, args: dict[str, Any],
        caller: CallerPrincipal = CallerPrincipal.JARVIS_LLM,
    ) -> dict[str, Any]:
        """Dispatch ``name`` and decode its output."""
        self.n += 1
        self.fx.dispatch(tool_request(name, f"A{self.n}", arguments=args, caller=caller))
        return dict(json.loads(tool_chain(self.fx, f"A{self.n}")["tool_output"]))


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """A world at :data:`THEN`."""
    made = World(tmp_path)
    yield made
    made.fx.close()


def test_get_current_time_is_the_clocks_moment_in_its_zone(world: World) -> None:
    """The date, the clock and the weekday are the context clock's, offset and all."""
    got = world.call("get_current_time", {}, CallerPrincipal.REGEX_ROUTER)
    assert got["iso"] == "2031-03-02T09:00:00+13:00"
    assert (got["date"], got["time"], got["weekday"]) == ("2031-03-02", "09:00", "Sunday")
    world.clock.advance(3 * 3600)
    assert world.call("get_current_time", {}, CallerPrincipal.REGEX_ROUTER)["time"] == "12:00"


def test_schedule_counts_today_and_the_lead_from_the_clock(world: World) -> None:
    """A thing at 15:00 today is today's, its ring is an hour ahead, and it is 6 h away."""
    made = world.call(
        "schedule", {"action": "add", "what": "Dentist", "kind": "go", "at": "2031-03-02 15:00"},
    )
    assert made["summary"].startswith('Kept "Dentist" for today (Sun) 3:00 pm')
    today = world.call("schedule", {"at": "today"})
    assert today["summary"].startswith("today (Sun Mar 2): 1 scheduled.")
    assert today["items"][0]["when"] == "today (Sun) 3:00 pm, in 6 h"
    world.clock.advance(24 * 3600)
    tomorrow = world.call("schedule", {"at": "today"})["summary"]
    assert tomorrow.startswith("today (Mon Mar 3): nothing scheduled")


def test_keep_stamps_the_day_the_clock_says(world: World) -> None:
    """A fact kept now is saved on the clock's day, not the machine's."""
    world.call("keep", {"text": "likes tea"})
    kept = world.call("kept", {})
    assert kept["items"] == [{"text": "likes tea", "when": "saved Mar 2"}]  # no year: this year


def test_plan_reads_the_clocks_day(world: World) -> None:
    """With no day named the plan is for the clock's today."""
    got = world.call("plan", {"action": "read"})
    assert got["day"] == "2031-03-02"


def test_the_conversation_and_archive_tools_name_the_days_the_clock_has(world: World) -> None:
    """"Today" and "yesterday" are the clock's: Sunday the 2nd and Saturday the 1st."""
    (conversation,) = (
        t for t in build_conversation_tool(world.memory) if t.name == "conversation"
    )
    here = tool_context(clock=world.clock)
    assert conversation.handler({"date": "today"}, here)["summary"] == (
        "Nothing was said on today (Sun)."
    )
    assert conversation.handler({"date": "yesterday"}, here)["summary"] == (
        "Nothing was said on yesterday (Sat)."
    )
    (archive,) = build_archive_tool(world.memory)
    assert archive.handler({"date": "today"}, here)["entries"] == []

