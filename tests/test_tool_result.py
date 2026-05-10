"""Tests for structured tool result helpers."""

from core.tool_result import (
    FAILURE,
    SUCCESS,
    make_tool_result,
    parse_tool_result,
    tool_message,
    tool_succeeded,
)


def test_make_and_parse_success_result() -> None:
    raw = make_tool_result(SUCCESS, "done", data={"id": "x"})
    parsed = parse_tool_result(raw)
    assert parsed["status"] == SUCCESS
    assert parsed["message"] == "done"
    assert parsed["data"] == {"id": "x"}
    assert tool_succeeded(raw) is True
    assert tool_message(raw) == "done"


def test_parse_legacy_failure_text() -> None:
    raw = "Device not found: desk_lamp"
    parsed = parse_tool_result(raw)
    assert parsed["status"] == FAILURE
    assert parsed["message"] == raw
    assert tool_succeeded(raw) is False
