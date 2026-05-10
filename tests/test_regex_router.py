"""Tests for core.regex_router."""

from __future__ import annotations

import pytest

from core.regex_router import RegexMatch, RegexRouter
from core.tool_result import FAILURE, SUCCESS, make_tool_result


def _minimal_config() -> dict:
    return {
        "regex_router": {
            "device_alias": {
                "灯带": "desk_lightstrip",
                "大灯": "bedroom_group",
                "电脑灯": "desk_lights",
                "灯": "all_lights",
            },
            "scene_alias": {
                "1": "2023-12-23",
                "2": "Hoho",
                "3": "lol",
                "4": "Ocean",
                "5": "pink",
                "6": "study",
                "7": "赛博朋克",
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


class TestFarewell:
    def setup_method(self) -> None:
        self.router = RegexRouter(_minimal_config())

    def test_match_zaijian(self) -> None:
        m = self.router.match("再见")
        assert m is not None
        assert m.pattern_id == "farewell"
        assert m.intent == "farewell"
        assert m.tool_name == ""  # no-op marker
        assert m.template_key == "farewell"

    def test_match_tuichu(self) -> None:
        m = self.router.match("退出")
        assert m is not None
        assert m.pattern_id == "farewell"

    def test_match_with_period(self) -> None:
        assert self.router.match("再见。") is not None
        assert self.router.match("退出.") is not None
        assert self.router.match("再见！") is not None

    def test_miss_substring(self) -> None:
        # Strict anchored — no substring match.
        assert self.router.match("在家再见") is None
        assert self.router.match("好的再见") is None
        assert self.router.match("再见了我先走") is None

    def test_miss_english(self) -> None:
        # English farewell phrases dropped — only 再见/退出.
        assert self.router.match("bye") is None
        assert self.router.match("goodbye") is None
        assert self.router.match("that's all") is None


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
        assert m.tool_name == "get_weather"

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
        assert m.tool_name == "obsidian_add_to_inbox"
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
        assert m.tool_args["device_id"] == "desk_lightstrip"
        assert m.tool_args["action"] == "turn_on"
        assert m.tool_args["matched_alias"] == "灯带"
        assert m.tool_args["resolution_source"] == "regex_router"
        assert m.template_vars == {"device": "灯带"}

    def test_turn_on_v_first_short(self) -> None:
        m = self.router.match("开大灯")
        assert m is not None
        assert m.tool_args["device_id"] == "bedroom_group"
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
        assert m.tool_args["device_id"] == "desk_lightstrip"
        assert m.tool_args["action"] == "turn_off"
        assert m.tool_args["matched_alias"] == "灯带"
        assert m.tool_args["resolution_source"] == "regex_router"

    def test_turn_off_v_last_guandiao(self) -> None:
        m = self.router.match("把灯带关掉")
        assert m is not None
        assert m.pattern_id == "smart_home_off_v_last"
        assert m.tool_args["action"] == "turn_off"

    def test_turn_off_v_last_guanle(self) -> None:
        m = self.router.match("把大灯关了")
        assert m is not None
        assert m.tool_args["device_id"] == "bedroom_group"
        assert m.tool_args["action"] == "turn_off"
        assert m.tool_args["matched_alias"] == "大灯"
        assert m.tool_args["resolution_source"] == "regex_router"

    def test_set_brightness(self) -> None:
        m = self.router.match("把灯带调到百分之60")
        assert m is not None
        assert m.pattern_id == "smart_home_set_brightness"
        assert m.tool_args["device_id"] == "desk_lightstrip"
        assert m.tool_args["action"] == "set_brightness"
        assert m.tool_args["value"] == "60"
        assert m.tool_args["matched_alias"] == "灯带"
        assert m.tool_args["resolution_source"] == "regex_router"
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


class TestCcSlashPatterns:
    def setup_method(self) -> None:
        self.router = RegexRouter(_minimal_config())

    def test_model_opus(self) -> None:
        m = self.router.match("cc切到opus")
        assert m is not None
        assert m.pattern_id == "cc_slash_model"
        assert m.tool_name == "cc_slash"
        assert m.tool_args == {"command": "model", "args": "opus"}
        assert m.template_vars == {"arg": "opus"}

    def test_model_sonnet(self) -> None:
        m = self.router.match("让cc切到sonnet")
        assert m is not None
        assert m.tool_args["args"] == "sonnet"

    def test_model_haiku(self) -> None:
        m = self.router.match("让 cc 切到haiku")
        assert m is not None
        assert m.tool_args["args"] == "haiku"

    def test_effort_xhigh(self) -> None:
        m = self.router.match("cc effort xhigh")
        assert m is not None
        assert m.pattern_id == "cc_slash_effort"
        assert m.tool_args == {"command": "effort", "args": "xhigh"}

    def test_effort_medium(self) -> None:
        m = self.router.match("让cc effort medium")
        assert m is not None
        assert m.tool_args["args"] == "medium"

    def test_bare_model_switch_misses(self) -> None:
        assert self.router.match("切到sonnet") is None

    def test_bare_effort_misses(self) -> None:
        assert self.router.match("effort medium") is None

    def test_miss_chinese_value(self) -> None:
        # "切到大模型" not in enum
        assert self.router.match("切到大模型") is None

    def test_miss_effort_chinese(self) -> None:
        # Chinese effort values intentionally fall-through
        assert self.router.match("effort 高") is None


class TestSceneActivatePattern:
    def setup_method(self) -> None:
        self.router = RegexRouter(_minimal_config())

    def test_scene_index_with_space(self) -> None:
        m = self.router.match("切到场景 1")
        assert m is not None
        assert m.pattern_id == "scene_activate"
        assert m.tool_name == "smart_home_control"
        assert m.tool_args["device_id"] == "scene"
        assert m.tool_args["action"] == "activate"
        assert m.tool_args["value"] == "2023-12-23"
        assert m.tool_args["matched_alias"] == "场景 1"
        assert m.tool_args["resolution_source"] == "regex_router"
        assert m.template_vars == {"scene": "2023-12-23"}

    def test_scene_index_no_space(self) -> None:
        m = self.router.match("切到场景6")
        assert m is not None
        assert m.tool_args["value"] == "study"

    def test_scene_jihuo_verb(self) -> None:
        m = self.router.match("激活场景7")
        assert m is not None
        assert m.tool_args["value"] == "赛博朋克"

    def test_scene_out_of_range(self) -> None:
        # Index 8 not configured -> regex strict [1-7] -> miss
        assert self.router.match("切到场景8") is None
        assert self.router.match("切到场景0") is None

    def test_scene_requires_changjing_word(self) -> None:
        # Bare "切到 1" no longer matches — must say "场景"
        assert self.router.match("切到 1") is None
        assert self.router.match("切到1") is None

    def test_scene_not_collide_with_cc_slash(self) -> None:
        # Explicit cc anchor must still hit cc_slash_model, not scene
        m = self.router.match("cc切到opus")
        assert m is not None
        assert m.pattern_id == "cc_slash_model"


class TestRenderResponse:
    def setup_method(self) -> None:
        config = {
            "regex_router": {
                "device_alias": {"灯带": "desk_lightstrip"},
                "templates": {
                    "get_current_time": ["现在{tool_result}。", "{tool_result}。"],
                    "smart_home_set_brightness": [
                        "好，{device}{value}%了。",
                        "调好了，{value}%。",
                    ],
                    "missing_var_template": ["{nonexistent}"],
                },
            },
        }
        self.router = RegexRouter(config)

    def test_render_with_tool_result(self) -> None:
        match = RegexMatch(
            pattern_id="get_current_time",
            intent="get_current_time",
            tool_name="get_current_time",
            template_key="get_current_time",
        )
        out = self.router.render_response(
            match,
            make_tool_result(SUCCESS, "下午两点"),
        )
        assert out in ("现在下午两点。", "下午两点。")

    def test_render_failure_uses_tool_message_not_success_template(self) -> None:
        match = RegexMatch(
            pattern_id="smart_home_turn_on",
            intent="smart_home_control",
            tool_name="smart_home_control",
            template_key="smart_home_turn_on",
            template_vars={"device": "灯"},
        )
        out = self.router.render_response(
            match,
            make_tool_result(FAILURE, "Device not found: all_lights"),
        )
        assert out == "Device not found: all_lights"

    def test_render_with_template_vars(self) -> None:
        match = RegexMatch(
            pattern_id="smart_home_set_brightness",
            intent="smart_home_control",
            tool_name="smart_home_control",
            template_key="smart_home_set_brightness",
            template_vars={"device": "灯带", "value": "60"},
        )
        out = self.router.render_response(match, "")
        assert out in ("好，灯带60%了。", "调好了，60%。")

    def test_render_unknown_template_key_falls_back_to_tool_result(self) -> None:
        match = RegexMatch(
            pattern_id="x",
            intent="i",
            tool_name="t",
            template_key="no_such_key",
        )
        assert self.router.render_response(match, "fallback text") == "fallback text"

    def test_render_template_missing_var_falls_back(self) -> None:
        match = RegexMatch(
            pattern_id="x",
            intent="i",
            tool_name="t",
            template_key="missing_var_template",
        )
        # Template references {nonexistent} but vars don't supply it
        assert self.router.render_response(match, "fallback") == "fallback"
