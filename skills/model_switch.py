"""Model switch skill — voice-controlled LLM preset switching."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)

# Chinese alias → preset name mapping
_ZH_ALIASES: dict[str, str] = {
    "快速": "fast",
    "快速模式": "fast",
    "深度": "deep",
    "深度模式": "deep",
    "聪明": "deep",
}


class ModelSwitchSkill(Skill):
    """Expose LLM preset switching as a voice-callable skill.

    Supports querying the current model, listing presets,
    and switching between configured presets (with Chinese aliases).
    """

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "model_switch"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "switch_model",
                "description": (
                    "Switch the LLM model preset, query current model status, "
                    "or list available presets. "
                    "Pass preset='' for current status, preset='list' to list "
                    "available presets, or a preset name like 'fast'/'deep' to switch."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "preset": {
                            "type": "string",
                            "description": (
                                "Preset name to switch to (e.g. 'fast', 'deep'), "
                                "'list' to show available presets, or empty string "
                                "for current status. Chinese aliases: "
                                "快速/快速模式→fast, 深度/深度模式/聪明→deep."
                            ),
                        },
                    },
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        """Execute a model switch tool call.

        Args:
            tool_name: The tool name (expected: ``switch_model``).
            tool_input: Dict with optional ``preset`` key.
            **context: Extra context (user_id, user_role, etc.).

        Returns:
            Human-readable result string.
        """
        preset = (tool_input.get("preset") or "").strip()

        # Query current status
        if not preset:
            active = self._llm.active_preset or "default"
            return f"当前模型: {self._llm.model} (preset={active})"

        # List available presets
        if preset == "list":
            presets = self._llm.get_presets()
            if not presets:
                return "没有配置模型预设。"
            active = self._llm.active_preset or "default"
            lines = []
            for name, cfg in presets.items():
                marker = " ← 当前" if name == active else ""
                lines.append(f"  {name}: {cfg.get('model', '?')}{marker}")
            return "可用预设:\n" + "\n".join(lines)

        # Resolve Chinese aliases
        resolved = _ZH_ALIASES.get(preset, preset)

        # Switch to preset
        try:
            msg = self._llm.switch_model(resolved)
            self.logger.info("Model switched to preset '%s'", resolved)
            return msg
        except ValueError as exc:
            return f"切换失败: {exc}"
