"""ADR 0190: the model reads the Mail page's unread list in one call."""

from __future__ import annotations

import json
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.mail_inbox_tool import build_mail_inbox_tool
from jarvis.execution.tools import ToolContext, ToolError
from jarvis.runtime.home import Home
from jarvis.shared import CallerPrincipal
from tests.integration.test_home_routes import (
    ZONE,
    _Connections,
    _Gmail,
    _Jev,
    _Microsoft,
    jev,  # noqa: F401 - the fixture.
)


def _tool(home: Home) -> Any:  # noqa: ANN401
    (tool,) = build_mail_inbox_tool(home.mail)
    return tool


def _call(home: Home) -> dict[str, Any]:
    return dict(_tool(home).handler({}, cast("ToolContext", None)))


def test_one_call_lists_the_marked_letters_without_junk_bodies_or_addresses(
    jev: _Jev,  # noqa: F811
) -> None:
    """Marks and importance ride along; the junk letter is counted, not listed; no addresses."""
    gmail = _Gmail()
    home = Home(
        _Connections(_Microsoft(), gmail),  # type: ignore[arg-type]
        (ZONE, ZoneInfo(ZONE)), None, jev.reply(importance=True),
    )
    tool = _tool(home)
    assert tool.risk_level == "L0"
    assert tool.read_only
    assert not tool.deferred
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert tool.description.isascii()
    result = _call(home)
    assert result["junk_left_out"] == 1  # the Shop Deals letter, 199a1c0d4105
    letters = {one["id"]: one for one in result["unread"]}
    assert "199a1c0d4105" not in letters
    assert len(letters) == 5
    assert letters["199a1c0d4101"] == {
        "id": "199a1c0d4101", "from": "Prof. Lee", "subject": "Office hours move to Thursday",
        "received": "2026-09-25T21:40:00+00:00", "reply": "yes",
        "importance": pytest.approx(2.2), "category": "school",
    }
    assert letters["199a1c0d4104"].get("reply") is None  # unrated reply is absent, not null
    # Only a sender with no display name shows its address, as on the Mail page; no address field.
    assert json.dumps(result).count("@") == 1
    assert all("address" not in one and "thread_id" not in one for one in letters.values())
    # One Home.mail read: one search and a metadata read per hit, no bodies.
    assert [name for name, _ in gmail.calls].count("gmail_search") == 1
    assert all(args.get("format") != "full" for _, args in gmail.calls)


def test_without_gmail_the_call_is_a_clear_tool_error() -> None:
    """Gmail not connected: an error that names it, which the model can relay."""
    home = Home(_Connections(_Microsoft(), None), (ZONE, ZoneInfo(ZONE)), None)  # type: ignore[arg-type]
    with pytest.raises(ToolError, match="Gmail is not connected") as caught:
        _call(home)
    assert caught.value.code == "unavailable"
