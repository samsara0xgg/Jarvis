"""Tests for ModelSwitchSkill."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from skills.model_switch import ModelSwitchSkill, _ZH_ALIASES


def _make_llm(
    model: str = "grok-4-1-fast",
    active_preset: str | None = "fast",
    presets: dict[str, dict[str, Any]] | None = None,
) -> MagicMock:
    """Build a mock LLMClient with configurable preset behaviour."""
    llm = MagicMock()
    llm.model = model
    llm.active_preset = active_preset
    if presets is None:
        presets = {
            "fast": {"model": "llama-3.3-70b-versatile"},
            "deep": {"model": "grok-4-1-fast-non-reasoning"},
        }
    llm.get_presets.return_value = dict(presets)

    def _switch(name: str) -> str:
        if name not in presets:
            raise ValueError(f"Unknown preset '{name}'. Available: {list(presets)}")
        llm.active_preset = name
        llm.model = presets[name]["model"]
        return f"已切换到 {name} 模式 (model={presets[name]['model']})"

    llm.switch_model.side_effect = _switch
    return llm


class TestModelSwitchSkill:
    """Tests for the ModelSwitchSkill class."""

    def test_skill_name(self) -> None:
        """skill_name is 'model_switch'."""
        skill = ModelSwitchSkill(_make_llm())
        assert skill.skill_name == "model_switch"

    def test_tool_definitions(self) -> None:
        """get_tool_definitions returns one tool named switch_model."""
        skill = ModelSwitchSkill(_make_llm())
        defs = skill.get_tool_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "switch_model"
        assert "preset" in defs[0]["input_schema"]["properties"]

    def test_query_current_model_empty_preset(self) -> None:
        """Empty preset returns current model status."""
        llm = _make_llm(model="grok-4-1-fast", active_preset="fast")
        skill = ModelSwitchSkill(llm)
        result = skill.execute("switch_model", {"preset": ""})
        assert "grok-4-1-fast" in result
        assert "fast" in result

    def test_query_current_model_no_preset_key(self) -> None:
        """Missing preset key returns current model status."""
        llm = _make_llm()
        skill = ModelSwitchSkill(llm)
        result = skill.execute("switch_model", {})
        assert "grok-4-1-fast" in result

    def test_query_current_model_no_active_preset(self) -> None:
        """No active preset shows 'default'."""
        llm = _make_llm(active_preset=None)
        skill = ModelSwitchSkill(llm)
        result = skill.execute("switch_model", {"preset": ""})
        assert "default" in result

    def test_list_presets(self) -> None:
        """preset='list' returns available presets."""
        skill = ModelSwitchSkill(_make_llm())
        result = skill.execute("switch_model", {"preset": "list"})
        assert "fast" in result
        assert "deep" in result
        assert "可用预设" in result

    def test_list_presets_marks_current(self) -> None:
        """preset='list' marks the currently active preset."""
        llm = _make_llm(active_preset="fast")
        skill = ModelSwitchSkill(llm)
        result = skill.execute("switch_model", {"preset": "list"})
        assert "当前" in result

    def test_list_presets_empty(self) -> None:
        """preset='list' with no presets configured."""
        llm = _make_llm(presets={})
        skill = ModelSwitchSkill(llm)
        result = skill.execute("switch_model", {"preset": "list"})
        assert "没有" in result

    def test_switch_to_preset(self) -> None:
        """Switching to a valid preset calls llm.switch_model."""
        llm = _make_llm()
        skill = ModelSwitchSkill(llm)
        result = skill.execute("switch_model", {"preset": "deep"})
        llm.switch_model.assert_called_once_with("deep")
        assert "deep" in result

    def test_switch_unknown_preset(self) -> None:
        """Unknown preset returns error message."""
        llm = _make_llm()
        skill = ModelSwitchSkill(llm)
        result = skill.execute("switch_model", {"preset": "nonexistent"})
        assert "切换失败" in result

    def test_chinese_alias_fast(self) -> None:
        """Chinese alias '快速' resolves to 'fast'."""
        llm = _make_llm()
        skill = ModelSwitchSkill(llm)
        skill.execute("switch_model", {"preset": "快速"})
        llm.switch_model.assert_called_once_with("fast")

    def test_chinese_alias_fast_mode(self) -> None:
        """Chinese alias '快速模式' resolves to 'fast'."""
        llm = _make_llm()
        skill = ModelSwitchSkill(llm)
        skill.execute("switch_model", {"preset": "快速模式"})
        llm.switch_model.assert_called_once_with("fast")

    def test_chinese_alias_deep(self) -> None:
        """Chinese alias '深度' resolves to 'deep'."""
        llm = _make_llm()
        skill = ModelSwitchSkill(llm)
        skill.execute("switch_model", {"preset": "深度"})
        llm.switch_model.assert_called_once_with("deep")

    def test_chinese_alias_deep_mode(self) -> None:
        """Chinese alias '深度模式' resolves to 'deep'."""
        llm = _make_llm()
        skill = ModelSwitchSkill(llm)
        skill.execute("switch_model", {"preset": "深度模式"})
        llm.switch_model.assert_called_once_with("deep")

    def test_chinese_alias_smart(self) -> None:
        """Chinese alias '聪明' resolves to 'deep'."""
        llm = _make_llm()
        skill = ModelSwitchSkill(llm)
        skill.execute("switch_model", {"preset": "聪明"})
        llm.switch_model.assert_called_once_with("deep")

    def test_zh_aliases_completeness(self) -> None:
        """All documented Chinese aliases are present."""
        expected = {"快速", "快速模式", "深度", "深度模式", "聪明"}
        assert set(_ZH_ALIASES.keys()) == expected


class TestEscalationKeywords:
    """Tests for per-turn escalation keyword constants."""

    def test_escalation_keywords_importable(self) -> None:
        """_ESCALATION_KEYWORDS is importable from jarvis module."""
        import importlib
        import jarvis as jarvis_mod
        importlib.reload(jarvis_mod)
        assert hasattr(jarvis_mod, "_ESCALATION_KEYWORDS")
        kws = jarvis_mod._ESCALATION_KEYWORDS
        assert "仔细想想" in kws
        assert "详细分析" in kws
        assert "认真想" in kws
        assert "好好想" in kws
