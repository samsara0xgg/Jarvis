"""Tests for core.regex_router."""

from __future__ import annotations

import pytest

from core.regex_router import RegexMatch, RegexRouter


def _minimal_config() -> dict:
    return {
        "regex_router": {
            "device_alias": {
                "灯带": "desk_lightstrip",
                "大灯": "bedroom_lamp_1",
                "电脑灯": "desk_lights",
                "灯": "all_lights",
            },
            "templates": {
                "get_current_time": ["现在{tool_result}。", "{tool_result}。"],
            },
        },
    }


class TestRegexMatch:
    def test_dataclass_frozen_with_defaults(self) -> None:
        m = RegexMatch(
            pattern_id="x",
            intent="i",
            tool_name="t",
        )
        assert m.pattern_id == "x"
        assert m.tool_args == {}
        assert m.template_vars == {}
        assert m.template_key == ""
        with pytest.raises(Exception):
            m.pattern_id = "y"  # type: ignore[misc]


class TestRegexRouterInit:
    def test_init_loads_config(self) -> None:
        router = RegexRouter(_minimal_config())
        assert router.device_alias["灯带"] == "desk_lightstrip"
        assert "get_current_time" in router.templates


class TestMatchGetCurrentTime:
    def setup_method(self) -> None:
        self.router = RegexRouter(_minimal_config())

    def test_match_canonical(self) -> None:
        m = self.router.match("现在几点了")
        assert m is not None
        assert m.pattern_id == "get_current_time"
        assert m.intent == "get_current_time"
        assert m.tool_name == "get_current_time"
        assert m.tool_args == {}
        assert m.template_key == "get_current_time"

    def test_match_no_le(self) -> None:
        # 了 optional
        assert self.router.match("现在几点") is not None

    def test_match_chinese_question_mark(self) -> None:
        assert self.router.match("现在几点了？") is not None

    def test_match_english_question_mark(self) -> None:
        assert self.router.match("现在几点?") is not None

    def test_miss_with_prefix(self) -> None:
        # Conversational prefix → fall-through
        assert self.router.match("我说现在几点了") is None

    def test_miss_with_suffix(self) -> None:
        assert self.router.match("现在几点了能见面") is None
