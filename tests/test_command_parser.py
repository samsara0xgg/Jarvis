"""Tests for the Hue-aware smart-home command parser."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.command_parser import COLOR_TEMP_MAP, COLOR_XY_MAP, CommandParser


def _load_config() -> dict:
    """Load the project config used by parser tests."""

    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


@pytest.fixture()
def parser() -> CommandParser:
    """Create a parser instance from the test config."""

    return CommandParser(_load_config())


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("打开卧室灯", {"device": "bedroom_light", "action": "turn_on"}),
        ("关掉客厅所有灯", {"device": "living_room_group", "action": "turn_off"}),
        (
            "把书房灯调到 50%",
            {"device": "study_light", "action": "set_brightness", "value": 50},
        ),
        (
            "把卧室灯调成暖光",
            {"device": "bedroom_light", "action": "set_color_temp", "value": "warm"},
        ),
        (
            "客厅灯变成蓝色",
            {"device": "living_room_light", "action": "set_color", "value": "blue"},
        ),
        (
            "切换到阅读模式",
            {"device": "scene", "action": "activate", "value": "阅读模式"},
        ),
        ("晚安", {"action": "voice_shortcut", "value": "晚安"}),
        ("我回来了", {"action": "voice_shortcut", "value": "我回来了"}),
    ],
)
def test_parse_supported_commands(
    parser: CommandParser,
    command: str,
    expected: dict[str, object],
) -> None:
    """The parser should recognize all supported command categories."""

    assert parser.parse(command) == expected


def test_parse_returns_error_for_unknown_command(parser: CommandParser) -> None:
    """Unknown utterances should return the configured error payload."""

    result = parser.parse("帮我放一首歌")

    assert result == {"error": "无法理解指令", "raw_text": "帮我放一首歌"}


def test_group_alias_takes_priority_over_shorter_light_alias(parser: CommandParser) -> None:
    """Longest alias resolution should prefer the living-room group phrase."""

    result = parser.parse("打开客厅所有灯")

    assert result == {"device": "living_room_group", "action": "turn_on"}


def test_color_and_color_temperature_maps_expose_expected_aliases() -> None:
    """Built-in Hue mappings should cover both Chinese and English aliases."""

    assert COLOR_XY_MAP["blue"] == COLOR_XY_MAP["蓝色"]
    assert COLOR_TEMP_MAP["暖光"] == "warm"
    assert COLOR_TEMP_MAP["cool"] == "cool"


@pytest.mark.parametrize("command", ["", "   \n\t  "])
def test_parse_empty_or_whitespace_input_returns_error(
    parser: CommandParser,
    command: str,
) -> None:
    """Empty or whitespace-only input should return the standard error payload."""

    assert parser.parse(command) == {"error": "无法理解指令", "raw_text": ""}


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "请把卧室灯设置为 warm",
            {"device": "bedroom_light", "action": "set_color_temp", "value": "warm"},
        ),
        (
            "把卧室灯变成 blue",
            {"device": "bedroom_light", "action": "set_color", "value": "blue"},
        ),
        ("  我 回 来 了 ", {"action": "voice_shortcut", "value": "我回来了"}),
    ],
)
def test_parse_mixed_language_and_spaced_aliases(
    parser: CommandParser,
    command: str,
    expected: dict[str, object],
) -> None:
    """The parser should handle mixed English aliases and normalized spacing."""

    assert parser.parse(command) == expected


def test_parse_garbled_text_returns_error_payload(parser: CommandParser) -> None:
    """Garbled text should fail gracefully without raising parser exceptions."""

    result = parser.parse("��@#卧室?灯")

    assert result == {"error": "无法理解指令", "raw_text": "��@#卧室?灯"}
