"""自动化规则管理 — 存储、触发、调度.

支持三种触发方式：
- keyword: 用户说特定词时触发（如"晚安"）
- cron: 定时触发（如每天7点）
- once: 一次性延时触发（如30分钟后）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

_DAYS_INFO: dict[str, tuple[str, str]] = {
    # key: (cron_expr, chinese_label)
    "everyday": ("*", "每天"),
    "weekdays": ("mon-fri", "工作日"),
    "weekends": ("sat,sun", "周末"),
    "monday": ("mon", "周一"),
    "tuesday": ("tue", "周二"),
    "wednesday": ("wed", "周三"),
    "thursday": ("thu", "周四"),
    "friday": ("fri", "周五"),
    "saturday": ("sat", "周六"),
    "sunday": ("sun", "周日"),
}


class AutomationRule:
    """一条自动化规则."""

    def __init__(
        self,
        name: str,
        trigger: dict[str, Any],
        actions: list[dict[str, Any]],
        created_at: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.trigger = trigger
        self.actions = actions
        self.created_at = created_at or datetime.now().isoformat()
        self.enabled = enabled

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trigger": self.trigger,
            "actions": self.actions,
            "created_at": self.created_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutomationRule:
        return cls(
            name=data["name"],
            trigger=data["trigger"],
            actions=data["actions"],
            created_at=data.get("created_at"),
            enabled=data.get("enabled", True),
        )


class AutomationRuleManager:
    """管理自动化规则：存储、keyword 匹配、scheduler 注册."""

    def __init__(
        self,
        rules_path: str | Path = "data/automation_rules.json",
        scheduler: Any = None,
        action_executor: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self.rules_path = Path(rules_path)
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self.scheduler = scheduler
        self.action_executor = action_executor
        self.logger = LOGGER

        self._rules: dict[str, AutomationRule] = {}
        self._load()
        self._register_all_scheduled()

    # --- 公开 API ---

    def create_rule(self, rule_data: dict[str, Any]) -> str:
        """从 Groq 返回的 rule JSON 创建规则.

        Args:
            rule_data: {"name": "...", "trigger": {...}, "actions": [...]}

        Returns:
            确认消息。
        """
        name = rule_data.get("name", "")
        if not name:
            return "规则名称不能为空。"

        trigger = rule_data.get("trigger", {})
        actions = rule_data.get("actions", [])

        if not trigger or not actions:
            return "规则缺少触发条件或动作。"

        rule = AutomationRule(name=name, trigger=trigger, actions=actions)
        self._rules[name] = rule
        self._save()

        # 注册定时任务
        self._register_scheduled(rule)

        self.logger.info("Rule created: %s (trigger: %s)", name, trigger.get("type"))
        return f"自动化规则「{name}」已创建。"

    def delete_rule(self, name: str) -> str:
        """删除规则."""
        if name not in self._rules:
            return f"没有找到规则「{name}」。"

        # 取消定时任务
        self._unregister_scheduled(name)

        del self._rules[name]
        self._save()
        self.logger.info("Rule deleted: %s", name)
        return f"规则「{name}」已删除。"

    def list_rules(self) -> str:
        """列出所有规则."""
        if not self._rules:
            return "目前没有自动化规则。"

        lines = [f"共 {len(self._rules)} 条规则："]
        for rule in self._rules.values():
            trigger_desc = self._describe_trigger(rule.trigger)
            status = "✅" if rule.enabled else "⏸️"
            action_count = len(rule.actions)
            lines.append(f"{status} {rule.name} — {trigger_desc}（{action_count} 个动作）")

        return "\n".join(lines)

    def check_keyword(self, text: str) -> tuple[list[dict[str, Any]], str] | None:
        """检查文本是否匹配 keyword 触发规则.

        只在文本以 keyword 开头或完全匹配时触发。

        Returns:
            (actions, rule_name) 元组，未匹配返回 None。
        """
        text_stripped = text.strip()
        for rule in self._rules.values():
            if not rule.enabled:
                continue
            if rule.trigger.get("type") != "keyword":
                continue

            keyword = rule.trigger.get("keyword", "")
            if not keyword:
                continue

            # 完全匹配或以 keyword 开头
            if text_stripped == keyword or text_stripped.startswith(keyword):
                self.logger.info("Keyword triggered: '%s' matched rule '%s'", text_stripped, rule.name)
                return rule.actions, rule.name

        return None

    # --- 内部方法 ---

    def _load(self) -> None:
        """从 JSON 文件加载规则."""
        if not self.rules_path.exists():
            return

        try:
            with open(self.rules_path) as f:
                data = json.load(f)
            for item in data.get("rules", []):
                rule = AutomationRule.from_dict(item)
                self._rules[rule.name] = rule
            self.logger.info("Loaded %d automation rules", len(self._rules))
        except Exception as exc:
            self.logger.error("Failed to load automation rules: %s", exc)

    def _save(self) -> None:
        """持久化规则到 JSON 文件."""
        try:
            data = {"rules": [r.to_dict() for r in self._rules.values()]}
            with open(self.rules_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            self.logger.error("Failed to save automation rules: %s", exc)

    def _register_all_scheduled(self) -> None:
        """启动时注册所有 cron/once 规则到 scheduler."""
        for rule in self._rules.values():
            self._register_scheduled(rule)

    def _register_scheduled(self, rule: AutomationRule) -> None:
        """注册单条规则到 scheduler."""
        if not self.scheduler or not self.scheduler.available:
            return
        if not rule.enabled:
            return

        trigger = rule.trigger
        trigger_type = trigger.get("type")

        if trigger_type == "cron":
            hour = trigger.get("hour", 7)
            minute = trigger.get("minute", 0)
            cron_days, _ = _DAYS_INFO.get(trigger.get("days", "everyday"), ("*", ""))
            days = cron_days

            self.scheduler.add_cron_job(
                job_id=f"auto_{rule.name}",
                func=lambda actions=rule.actions: self._execute_actions(actions),
                hour=str(hour),
                minute=str(minute),
                day_of_week=days,
            )
            self.logger.info("Scheduled cron: %s at %02d:%02d (%s)", rule.name, hour, minute, days)

        elif trigger_type == "once":
            delay_minutes = trigger.get("delay_minutes", 30)
            run_time = datetime.now() + timedelta(minutes=delay_minutes)

            self.scheduler.add_date_job(
                job_id=f"auto_{rule.name}",
                func=lambda actions=rule.actions, name=rule.name: self._execute_once(actions, name),
                run_date=run_time,
            )
            self.logger.info("Scheduled once: %s in %d minutes", rule.name, delay_minutes)

    def _unregister_scheduled(self, rule_name: str) -> None:
        """从 scheduler 移除规则."""
        if not self.scheduler or not self.scheduler.available:
            return
        try:
            self.scheduler.remove_job(f"auto_{rule_name}")
        except (KeyError, LookupError):
            pass  # job 未注册（keyword-only 规则）

    def _execute_actions(self, actions: list[dict[str, Any]]) -> None:
        """执行规则的 actions."""
        if self.action_executor:
            self.action_executor(actions)
        else:
            self.logger.warning("No action executor configured")

    def _execute_once(self, actions: list[dict[str, Any]], rule_name: str) -> None:
        """执行一次性规则后自动删除."""
        self._execute_actions(actions)
        if rule_name in self._rules:
            del self._rules[rule_name]
            self._save()
            self.logger.info("One-time rule '%s' executed and removed", rule_name)

    def _describe_trigger(self, trigger: dict[str, Any]) -> str:
        """生成触发条件的中文描述."""
        trigger_type = trigger.get("type", "unknown")

        if trigger_type == "keyword":
            return f"说「{trigger.get('keyword', '?')}」时触发"
        elif trigger_type == "cron":
            hour = trigger.get("hour", 0)
            minute = trigger.get("minute", 0)
            days_key = trigger.get("days", "everyday")
            _, days_cn = _DAYS_INFO.get(days_key, ("*", days_key))
            return f"{days_cn} {hour:02d}:{minute:02d}"
        elif trigger_type == "once":
            return f"{trigger.get('delay_minutes', 0)} 分钟后"
        else:
            return f"未知触发: {trigger_type}"
