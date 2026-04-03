"""Health skill — voice-queryable system health status."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)

# Human-readable Chinese labels for component prefixes.
_COMPONENT_LABELS: dict[str, str] = {
    "tts": "语音合成",
    "intent": "意图路由",
    "llm": "语言模型",
    "asr": "语音识别",
}


def _friendly_name(component: str) -> str:
    """Convert 'tts.openai' → '语音合成(OpenAI)'."""
    parts = component.split(".", 1)
    prefix_label = _COMPONENT_LABELS.get(parts[0], parts[0])
    engine = parts[1].upper() if len(parts) > 1 else ""
    return f"{prefix_label}({engine})" if engine else prefix_label


class HealthSkill(Skill):
    """Report system component health via voice query."""

    def __init__(self, tracker: Any) -> None:
        self._tracker = tracker
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "health"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_system_health",
                "description": (
                    "获取系统各组件的健康状态。"
                    "用户问系统状态、健康状况、哪个模块挂了时使用。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "component": {
                            "type": "string",
                            "description": "可选：查询特定组件，如 tts.openai、intent.groq。",
                        },
                    },
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        if tool_name != "get_system_health":
            return f"Unknown health tool: {tool_name}"

        component = tool_input.get("component", "").strip()
        if component:
            return self._single_component(component)
        return self._full_summary()

    def _single_component(self, component: str) -> str:
        state = self._tracker.get_status(component)
        name = _friendly_name(component)
        if state.status.value == "healthy":
            return f"{name}运行正常。"
        status_cn = "已降级" if state.status.value == "degraded" else "不可用"
        error_info = f"，错误: {state.last_error}" if state.last_error else ""
        return f"{name}{status_cn}（连续失败{state.consecutive_failures}次{error_info}）。"

    def _full_summary(self) -> str:
        summary = self._tracker.get_health_summary()
        if summary["is_healthy"]:
            return "所有系统正常运行。"

        parts: list[str] = []
        for comp in summary["degraded"]:
            info = summary["components"][comp]
            name = _friendly_name(comp)
            error = f"({info['last_error']})" if info.get("last_error") else ""
            parts.append(f"{name}已降级{error}")
        for comp in summary["unavailable"]:
            info = summary["components"][comp]
            name = _friendly_name(comp)
            error = f"({info['last_error']})" if info.get("last_error") else ""
            parts.append(f"{name}不可用{error}")

        healthy_count = len(summary["healthy"])
        issue_str = "，".join(parts)
        return f"{issue_str}。其余{healthy_count}个组件正常。"
