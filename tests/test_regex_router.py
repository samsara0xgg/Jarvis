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


class TestNoArgPatterns:
    def setup_method(self) -> None:
        self.router = RegexRouter(_minimal_config())

    def test_get_date(self) -> None:
        m = self.router.match("今天几号")
        assert m is not None
        assert m.pattern_id == "get_date"
        assert m.intent == "get_date"
        # reuses get_current_time tool — no separate get_date python tool exists
        assert m.tool_name == "get_current_time"
        assert m.template_key == "get_date"

    def test_weather(self) -> None:
        m = self.router.match("今天天气怎么样")
        assert m is not None
        assert m.pattern_id == "weather"
        assert m.tool_name == "weather"

    def test_list_todos_daiban(self) -> None:
        m = self.router.match("我有什么待办")
        assert m is not None
        assert m.pattern_id == "list_todos"

    def test_list_todos_english(self) -> None:
        m = self.router.match("我有什么todo")
        assert m is not None
        assert m.pattern_id == "list_todos"

    def test_cc_interrupt(self) -> None:
        m = self.router.match("停cc")
        assert m is not None
        assert m.pattern_id == "cc_interrupt"
        assert m.tool_name == "cc_interrupt"

    def test_miss_unrelated(self) -> None:
        assert self.router.match("讲个故事") is None


class TestContentCapturePatterns:
    def setup_method(self) -> None:
        self.router = RegexRouter(_minimal_config())

    def test_add_todo_colon(self) -> None:
        m = self.router.match("加个todo: 买牛奶")
        assert m is not None
        assert m.pattern_id == "add_todo"
        assert m.tool_args == {"content": "买牛奶"}

    def test_add_todo_chinese_colon(self) -> None:
        m = self.router.match("加个todo：写代码")
        assert m is not None
        assert m.tool_args == {"content": "写代码"}

    def test_add_todo_space(self) -> None:
        m = self.router.match("加个todo 跑步")
        assert m is not None
        assert m.tool_args == {"content": "跑步"}

    def test_obsidian_inbox(self) -> None:
        m = self.router.match("记到inbox 想个新项目")
        assert m is not None
        assert m.pattern_id == "obsidian_inbox"
        assert m.tool_args == {"content": "想个新项目"}

    def test_cc_tell(self) -> None:
        m = self.router.match("给cc发 下一步是什么")
        assert m is not None
        assert m.pattern_id == "cc_tell"
        assert m.tool_args == {"text": "下一步是什么"}

    def test_type_to_focused_colon(self) -> None:
        m = self.router.match("帮我输入: hello world")
        assert m is not None
        assert m.pattern_id == "type_to_focused"
        assert m.tool_args == {"text": "hello world"}

    def test_type_to_focused_chinese_colon(self) -> None:
        m = self.router.match("帮我输入:你好")
        assert m is not None
        assert m.tool_args == {"text": "你好"}

    def test_cc_tell_miss_no_content(self) -> None:
        # \s+(.+) requires at least one whitespace + char
        assert self.router.match("给cc发") is None

    def test_add_todo_miss_no_content(self) -> None:
        assert self.router.match("加个todo") is None


class TestSetTimerPattern:
    def setup_method(self) -> None:
        self.router = RegexRouter(_minimal_config())

    def test_set_timer_basic(self) -> None:
        m = self.router.match("5分钟提醒我")
        assert m is not None
        assert m.pattern_id == "set_timer"
        assert m.intent == "set_timer"
        assert m.tool_name == "set_timer"
        assert m.tool_args == {"seconds": 300, "label": "timer"}
        assert m.template_vars == {"minutes": "5"}

    def test_set_timer_with_hou(self) -> None:
        m = self.router.match("10分钟后提醒我")
        assert m is not None
        assert m.tool_args["seconds"] == 600

    def test_set_timer_with_space(self) -> None:
        m = self.router.match("3 分钟提醒我")
        assert m is not None
        assert m.tool_args["seconds"] == 180

    def test_set_timer_miss_with_label(self) -> None:
        # Label after "提醒我" not in pattern → fall-through
        assert self.router.match("5分钟提醒我喝水") is None

    def test_set_timer_miss_no_minutes_word(self) -> None:
        assert self.router.match("5提醒我") is None


class TestSmartHomePatterns:
    def setup_method(self) -> None:
        self.router = RegexRouter(_minimal_config())

    def test_turn_on_v_first_with_lighstrip(self) -> None:
        m = self.router.match("打开灯带")
        assert m is not None
        assert m.pattern_id == "smart_home_on_v_first"
        assert m.tool_name == "smart_home_control"
        assert m.tool_args == {"device_id": "desk_lightstrip", "action": "turn_on"}
        assert m.template_vars == {"device": "灯带"}

    def test_turn_on_v_first_short(self) -> None:
        m = self.router.match("开大灯")
        assert m is not None
        assert m.tool_args["device_id"] == "bedroom_lamp_1"
        assert m.tool_args["action"] == "turn_on"

    def test_turn_on_v_first_dianlao(self) -> None:
        m = self.router.match("打开电脑灯")
        assert m is not None
        assert m.tool_args["device_id"] == "desk_lights"

    def test_turn_on_v_first_single_char(self) -> None:
        # "灯" alone → all_lights
        m = self.router.match("开灯")
        assert m is not None
        assert m.tool_args["device_id"] == "all_lights"

    def test_turn_on_v_last(self) -> None:
        m = self.router.match("把灯带打开")
        assert m is not None
        assert m.pattern_id == "smart_home_on_v_last"
        assert m.tool_args["device_id"] == "desk_lightstrip"
        assert m.tool_args["action"] == "turn_on"

    def test_turn_off_v_first(self) -> None:
        m = self.router.match("关灯带")
        assert m is not None
        assert m.pattern_id == "smart_home_off_v_first"
        assert m.tool_args == {"device_id": "desk_lightstrip", "action": "turn_off"}

    def test_turn_off_v_last_guandiao(self) -> None:
        m = self.router.match("把灯带关掉")
        assert m is not None
        assert m.pattern_id == "smart_home_off_v_last"
        assert m.tool_args["action"] == "turn_off"

    def test_turn_off_v_last_guanle(self) -> None:
        m = self.router.match("把大灯关了")
        assert m is not None
        assert m.tool_args == {"device_id": "bedroom_lamp_1", "action": "turn_off"}

    def test_set_brightness(self) -> None:
        m = self.router.match("把灯带调到百分之60")
        assert m is not None
        assert m.pattern_id == "smart_home_set_brightness"
        assert m.tool_args == {
            "device_id": "desk_lightstrip",
            "action": "set_brightness",
            "value": "60",
        }
        assert m.template_vars == {"device": "灯带", "value": "60"}

    def test_alias_priority_zhuangshi_over_zhuang(self) -> None:
        # "灯带" is checked before "灯" in alternation; "开灯带" must match "灯带"
        m = self.router.match("开灯带")
        assert m is not None
        assert m.tool_args["device_id"] == "desk_lightstrip"

    def test_miss_unknown_alias(self) -> None:
        # 卧室灯 not in DEVICE_ALIAS → fall-through
        assert self.router.match("打开卧室灯") is None

    def test_miss_brightness_no_baifenzhi(self) -> None:
        # Without "百分之" → fall-through
        assert self.router.match("把灯带调到60") is None
