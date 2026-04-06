"""本地执行器 — 根据路由器返回的结构化 JSON 执行操作."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from core.automation_rules import AutomationRuleManager

LOGGER = logging.getLogger(__name__)


class Action(Enum):
    """执行结果的处理方式."""

    RESPONSE = "response"  # 直接 TTS 播报，不过 LLM（零随机）
    REQLLM = "reqllm"      # 把数据交给 LLM 用小月语气转述


@dataclass
class ActionResponse:
    """本地执行器的统一返回类型."""

    action: Action
    text: str


class LocalExecutor:
    """执行路由器解析好的结构化指令."""

    def __init__(self, skill_registry: Any, rule_manager: AutomationRuleManager | None = None) -> None:
        self.skill_registry = skill_registry
        self.rule_manager = rule_manager
        self.logger = LOGGER

    def execute_smart_home(
        self, actions: list[dict], user_role: str = "owner", response: str | None = None,
    ) -> ActionResponse:
        """执行 smart_home actions.

        Args:
            actions: 路由器返回的 actions 列表，每项包含 device_id/action/value。
            user_role: 用户角色。
            response: 路由器生成的回复文本。

        Returns:
            ActionResponse — 直接播报。
        """
        if not actions:
            return ActionResponse(Action.RESPONSE, response or "没有需要执行的操作。")

        results = []
        for act in actions:
            device_id = act.get("device_id", "")
            action = act.get("action", "")
            value = act.get("value")

            if not device_id or not action:
                continue

            tool_input: dict[str, Any] = {"device_id": device_id, "action": action}
            if value is not None:
                tool_input["value"] = value

            result = self.skill_registry.execute(
                "smart_home_control", tool_input, user_role=user_role,
            )
            self.logger.info("Execute: %s → %s", tool_input, result)
            results.append(result)

        if not results:
            return ActionResponse(Action.RESPONSE, "没有需要执行的操作。")

        errors = [r for r in results if "Error" in r or "denied" in r or "not found" in r]
        if errors:
            return ActionResponse(Action.RESPONSE, f"部分操作失败：{'; '.join(errors)}")

        # Always use template — Groq response can contain ASR garbage or hallucinations
        return ActionResponse(Action.RESPONSE, self._build_smart_home_reply(actions))

    @staticmethod
    def _build_smart_home_reply(actions: list[dict]) -> str:
        """Build a natural Chinese reply based on actually executed actions."""
        _ACTION_TEMPLATES = {
            "turn_on": "开了",
            "turn_off": "关了",
            "set_brightness": "亮度调到{value}%了",
            "set_color": "颜色调成{value}了",
            "set_color_temp": "色温调成{value}了",
            "set_effect": "特效设为{value}了",
            "lock": "已上锁",
            "unlock": "已解锁",
            "set_temperature": "温度设为{value}度了",
            "activate": "已激活",
        }
        parts = []
        for act in actions:
            action = act.get("action", "")
            value = act.get("value")
            tmpl = _ACTION_TEMPLATES.get(action, "已执行")
            if value is not None and "{value}" in tmpl:
                parts.append(tmpl.format(value=value))
            else:
                parts.append(tmpl)
        action_text = "，".join(parts)
        return f"好的，{action_text}。"

    def execute_info_query(
        self, sub_type: str | None, query: Any, user_role: str = "owner",
    ) -> ActionResponse:
        """执行信息查询.

        Args:
            sub_type: 查询子类型（stocks/news/weather）。
            query: 查询参数。
            user_role: 用户角色。

        Returns:
            ActionResponse — REQLLM，让 LLM 用小月语气转述数据。
        """
        result: str | None = None

        if sub_type == "stocks":
            symbols = query if isinstance(query, list) else None
            tool_input = {"symbols": symbols} if symbols else {}
            result = self.skill_registry.execute(
                "get_stock_watchlist", tool_input, user_role=user_role,
            )

        elif sub_type == "news":
            focus = query if isinstance(query, str) else "all"
            result = self.skill_registry.execute(
                "get_news_briefing", {"focus": focus}, user_role=user_role,
            )

        elif sub_type == "weather":
            result = self.skill_registry.execute(
                "get_weather", {}, user_role=user_role,
            )

        if not result:
            return ActionResponse(Action.RESPONSE, "没查到相关信息。")

        return ActionResponse(Action.REQLLM, result)

    def execute_time(self, sub_type: str | None) -> ActionResponse:
        """处理时间查询."""
        now = datetime.now()
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekdays[now.weekday()]

        if sub_type in ("date", "weekday"):
            text = f"今天是{now.year}年{now.month}月{now.day}日，{weekday}。"
        else:
            text = f"现在是{now.hour}点{now.minute:02d}分。"

        return ActionResponse(Action.RESPONSE, text)

    def execute_automation(self, sub_type: str | None, rule: dict[str, Any] | None) -> ActionResponse:
        """处理自动化规则操作.

        Args:
            sub_type: create / list / delete
            rule: Groq 返回的 rule JSON（仅 create 时需要）

        Returns:
            ActionResponse — 直接播报。
        """
        if not self.rule_manager:
            return ActionResponse(Action.RESPONSE, "自动化功能未启用。")

        if sub_type == "create":
            if not rule:
                return ActionResponse(Action.RESPONSE, "缺少规则信息。")
            return ActionResponse(Action.RESPONSE, self.rule_manager.create_rule(rule))

        if sub_type == "list":
            return ActionResponse(Action.RESPONSE, self.rule_manager.list_rules())

        if sub_type == "delete":
            name = rule.get("name", "") if rule else ""
            if not name:
                return ActionResponse(Action.RESPONSE, "请指定要删除的规则名称。")
            return ActionResponse(Action.RESPONSE, self.rule_manager.delete_rule(name))

        return ActionResponse(Action.RESPONSE, "不支持的自动化操作。")

    def execute_skill_alias(
        self, actions: list[dict], user_role: str = "owner",
    ) -> ActionResponse:
        """执行 skill_alias actions — 调用指定 skill 的指定 tool.

        Args:
            actions: 包含 skill/tool/params 的 action 列表。
            user_role: 用户角色。

        Returns:
            ActionResponse — REQLLM，让 LLM 用小月语气转述结果。
        """
        results = []
        for act in actions:
            tool_name = act.get("tool", "")
            params = act.get("params", {})
            if not tool_name:
                continue
            result = self.skill_registry.execute(
                tool_name, params, user_role=user_role,
            )
            self.logger.info("Skill alias execute: %s(%s) → %s", tool_name, params, result[:80])
            results.append(result)

        if not results:
            return ActionResponse(Action.RESPONSE, "没有需要执行的操作。")

        return ActionResponse(Action.REQLLM, "\n".join(results))

