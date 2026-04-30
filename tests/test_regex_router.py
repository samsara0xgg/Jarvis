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
