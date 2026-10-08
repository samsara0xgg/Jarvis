"""ADR 0115: a fixed line under her while a tool is really running, from the action rows alone."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import inherent_loop
from jarvis.runtime.tool_status import SHOW_AFTER_S, Shown, ToolStatus, plan
from jarvis.shared import lang
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.shared import Event


@pytest.fixture(autouse=True)
def _english() -> Iterator[None]:
    before = lang.language()
    lang.set_language("en")
    yield
    lang.set_language(before)


@pytest.mark.parametrize(
    ("tool", "line", "after"),
    [
        ("web_search", "Searching the web...", 0.0),
        ("web_fetch", "Reading the page...", 0.0),
        ("screen_look", "Looking at your screen...", 0.0),
        ("refresh_work_state", "Updating your work state...", 0.0),
        ("daily_work_report", "Writing your daily report...", 0.0),
        ("mcp__microsoft__get-calendar-view", "Checking your calendar...", SHOW_AFTER_S),
        ("mcp__gmail__gmail_search", "Checking your mail...", SHOW_AFTER_S),
        ("mcp__notion__notion-search", "Searching Notion...", SHOW_AFTER_S),
        ("mcp__gmail__gmail_send", "Working on it...", SHOW_AFTER_S),
        ("mcp__hue__list_lights", "Working on it...", SHOW_AFTER_S),
        ("query_activity", "Working on it...", SHOW_AFTER_S),
        ("tool_search", "Working on it...", SHOW_AFTER_S),
        ("", "Working on it...", SHOW_AFTER_S),
    ],
)
def test_the_table_maps_a_tool_to_its_fixed_line_and_when_it_shows(
    tool: str, line: str, after: float,
) -> None:
    """The slow list shows at once; MCP reads and the rest only once they are slow."""
    key, delay = plan(tool)
    assert (lang.t(key), delay) == (line, after)


def test_every_line_has_a_zh_and_an_en_wording() -> None:
    """Nine kinds of work, each written in both languages."""
    tools = (
        "web_search", "web_fetch", "screen_look", "refresh_work_state", "daily_work_report",
        "mcp__microsoft__get_x", "mcp__gmail__get_x", "mcp__notion__get_x", "other",
    )
    keys = {plan(t)[0] for t in tools}
    assert len(keys) == len(tools)
    for key in keys:
        assert lang.t(key, lang="zh")
        assert lang.t(key, lang="en")


def _row(conn: sqlite3.Connection, type_: str, **payload: object) -> Event:
    return emit_event(conn, type=type_, payload=payload, correlation={"turn_id": "T1"})


def test_the_line_follows_running_to_result_and_the_turn_end(tmp_path: Path) -> None:
    """Running shows it, a result or the turn's end clears it, an instant tool never shows."""
    conn = open_event_log(tmp_path / "events.db")
    status = ToolStatus()

    def feed(type_: str, now: float, **payload: object) -> None:
        status.feed(_row(conn, type_, **payload), now)

    def propose(action_id: str, tool: str, now: float) -> None:
        feed("action.proposed", now, action_id=action_id, tool_name=tool,
             caller_principal="x", risk_level="low")

    propose("A1", "web_search", 0)
    assert status.shown(0) is None  # proposed is not running
    feed("action.running", 1, action_id="A1")
    assert status.shown(1) == Shown("T1", "tool_status.web")
    feed("action.result_observed", 3, action_id="A1", semantics="x")
    assert status.shown(3) is None

    propose("A2", "query_activity", 4)
    feed("action.running", 4, action_id="A2")
    assert status.shown(4.2) is None  # an instant tool never flashes
    assert status.shown(4 + SHOW_AFTER_S) == Shown("T1", "tool_status.generic")
    feed("action.failed", 6, action_id="A2")
    assert status.shown(6) is None

    propose("A3", "screen_look", 7)
    feed("action.running", 7, action_id="A3")
    propose("A4", "web_search", 8)
    feed("action.running", 8, action_id="A4")
    assert status.shown(8) == Shown("T1", "tool_status.web")  # two at once: the latest
    feed("action.result_observed", 9, action_id="A4", semantics="x")
    assert status.shown(9) == Shown("T1", "tool_status.screen")
    feed("turn.ended", 10, turn_id="T1")
    assert status.shown(10) is None


class _Wire:
    """The broadcaster calls the watcher makes, recorded."""

    has_clients = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def broadcast_op(self, op: str, **payload: object) -> None:
        self.sent.append((op, payload))


async def _watch(conn: sqlite3.Connection, wire: _Wire, steps: list[Any]) -> None:
    task = asyncio.create_task(
        inherent_loop._tool_status_watcher(  # noqa: SLF001 - the production watcher
            SimpleNamespace(conn=conn), wire, poll_interval_s=0.005,  # type: ignore[arg-type]
        ),
    )
    await asyncio.sleep(0.05)
    for step in steps:
        step()
        await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_the_watcher_sends_the_line_on_running_and_clears_it_on_the_result(tmp_path: Path) -> None:
    """A real event log: a slow tool gets one line and one clear; an instant tool gets nothing."""
    conn = open_event_log(tmp_path / "events.db")
    wire = _Wire()

    def propose(action_id: str, tool: str) -> Any:  # noqa: ANN401 - a step
        return lambda: _row(
            conn, "action.proposed", action_id=action_id, tool_name=tool,
            caller_principal="x", risk_level="low",
        )

    asyncio.run(_watch(conn, wire, [
        propose("A1", "get_current_time"),
        lambda: _row(conn, "action.running", action_id="A1"),
        lambda: _row(conn, "action.result_observed", action_id="A1", semantics="x"),
        propose("A2", "web_search"),
        lambda: _row(conn, "action.running", action_id="A2"),
        lambda: _row(conn, "turn.ended", turn_id="T1"),
    ]))
    assert wire.sent == [
        ("tool", {"turn_id": "T1", "label": "Searching the web..."}),
        ("tool", {"turn_id": "T1", "label": ""}),
    ]


def test_the_watcher_keeps_the_line_between_two_tools_and_clears_it_when_the_turn_ends(
    tmp_path: Path,
) -> None:
    """A tool's result alone sends no clear (no flicker); the turn's end does."""
    conn = open_event_log(tmp_path / "events.db")
    wire = _Wire()

    def propose(action_id: str, tool: str) -> Any:  # noqa: ANN401 - a step
        return lambda: _row(
            conn, "action.proposed", action_id=action_id, tool_name=tool,
            caller_principal="x", risk_level="low",
        )

    asyncio.run(_watch(conn, wire, [
        propose("A1", "web_search"),
        lambda: _row(conn, "action.running", action_id="A1"),
        lambda: _row(conn, "action.result_observed", action_id="A1", semantics="x"),
        propose("A2", "web_fetch"),
        lambda: _row(conn, "action.running", action_id="A2"),
        lambda: _row(conn, "action.result_observed", action_id="A2", semantics="x"),
        lambda: _row(conn, "turn.ended", turn_id="T1"),
    ]))
    assert wire.sent == [
        ("tool", {"turn_id": "T1", "label": "Searching the web..."}),
        ("tool", {"turn_id": "T1", "label": "Reading the page..."}),
        ("tool", {"turn_id": "T1", "label": ""}),
    ]
