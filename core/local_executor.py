"""本地执行器 — 根据路由器返回的结构化 JSON 执行操作."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.automation_rules import AutomationRuleManager

LOGGER = logging.getLogger(__name__)


class LocalExecutor:
    """执行路由器解析好的结构化指令."""

    def __init__(self, skill_registry: Any, rule_manager: AutomationRuleManager | None = None) -> None:
        self.skill_registry = skill_registry
        self.rule_manager = rule_manager
        self.logger = LOGGER

    def execute_smart_home(self, actions: list[dict], user_role: str = "owner") -> str | None:
        """执行 smart_home actions.

        Args:
            actions: 路由器返回的 actions 列表，每项包含 device_id/action/value。
            user_role: 用户角色。

        Returns:
            执行结果文本，全部失败返回 None。
        """
        if not actions:
            return None

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
            return None

        # 有任何错误就返回错误信息
        errors = [r for r in results if "Error" in r or "denied" in r or "not found" in r]
        if errors:
            return f"部分操作失败：{'; '.join(errors)}"

        return None  # 成功时用路由器的 response，不用这里生成

    def execute_info_query(self, sub_type: str | None, query: Any, user_role: str = "owner") -> str | None:
        """执行信息查询.

        Args:
            sub_type: 查询子类型（stocks/news/weather）。
            query: 查询参数。
            user_role: 用户角色。

        Returns:
            查询结果文本。
        """
        if sub_type == "stocks":
            symbols = query if isinstance(query, list) else None
            tool_input = {"symbols": symbols} if symbols else {}
            return self.skill_registry.execute(
                "get_stock_watchlist", tool_input, user_role=user_role,
            )

        if sub_type == "news":
            focus = query if isinstance(query, str) else "all"
            return self.skill_registry.execute(
                "get_news_briefing", {"focus": focus}, user_role=user_role,
            )

        if sub_type == "weather":
            return self.skill_registry.execute(
                "get_weather", {}, user_role=user_role,
            )

        return None

    def execute_time(self, sub_type: str | None) -> str:
        """处理时间查询."""
        now = datetime.now()
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekdays[now.weekday()]

        if sub_type in ("date", "weekday"):
            return f"今天是{now.year}年{now.month}月{now.day}日，{weekday}。"

        return f"现在是{now.hour}点{now.minute:02d}分。"

    def execute_automation(self, sub_type: str | None, rule: dict[str, Any] | None) -> str | None:
        """处理自动化规则操作.

        Args:
            sub_type: create / list / delete
            rule: Groq 返回的 rule JSON（仅 create 时需要）

        Returns:
            操作结果文本。
        """
        if not self.rule_manager:
            return "自动化功能未启用。"

        if sub_type == "create":
            if not rule:
                return "缺少规则信息。"
            return self.rule_manager.create_rule(rule)

        if sub_type == "list":
            return self.rule_manager.list_rules()

        if sub_type == "delete":
            name = rule.get("name", "") if rule else ""
            if not name:
                return "请指定要删除的规则名称。"
            return self.rule_manager.delete_rule(name)

        return None

