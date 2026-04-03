"""End-to-end tests for T1 skill learning system."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.learning_router import LearningRouter
from core.skill_loader import SkillLoader
from core.skill_factory import SkillFactory


_VALID_SKILL = '''
from skills import Skill
from typing import Any

class FlightSkill(Skill):
    @property
    def skill_name(self) -> str:
        return "flight"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [{"name": "check_flight", "description": "Check flight info",
                 "input_schema": {"type": "object", "properties": {"route": {"type": "string"}}}}]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **ctx: Any) -> str:
        return f"Flight info for {tool_input.get('route', 'unknown')}"
'''


class TestLearningRouterToLoader:
    def test_create_detected_then_loaded(self, tmp_path):
        router = LearningRouter(skill_names=["weather", "time"])
        intent = router.detect("学会查航班信息")
        assert intent is not None
        assert intent.mode == "create"

        learned_dir = tmp_path / "skills" / "learned"
        learned_dir.mkdir(parents=True)
        (learned_dir / "flight_skill.py").write_text(_VALID_SKILL)

        loader = SkillLoader(str(learned_dir))
        skills = loader.scan()
        assert len(skills) == 1
        assert skills[0].skill_name == "flight"

        result = skills[0].execute("check_flight", {"route": "YVR-PEK"})
        assert "YVR-PEK" in result


class TestSecurityScanIntegration:
    def test_factory_rejects_dangerous_code(self, tmp_path):
        (tmp_path / "skills" / "learned").mkdir(parents=True)
        factory = SkillFactory(
            learned_dir=str(tmp_path / "skills" / "learned"),
            project_root=str(tmp_path),
        )
        evil_path = tmp_path / "skills" / "learned" / "evil.py"
        evil_path.write_text('import os; os.system("rm -rf /")')
        errors = factory._security_scan(str(evil_path))
        assert len(errors) > 0


class TestSkillAliasFlow:
    def test_detect_config_then_create_rule(self):
        from core.automation_rules import AutomationRuleManager

        router = LearningRouter(skill_names=["realtime_data"])
        intent = router.detect("以后我说收盘就帮我查股票")
        assert intent is not None
        assert intent.mode == "config"
        assert "收盘" in intent.trigger

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            rules_path = f.name

        mgr = AutomationRuleManager(rules_path=rules_path, scheduler=MagicMock(available=False))
        result = mgr.create_rule({
            "name": "收盘快捷",
            "trigger": {"type": "skill_alias", "keyword": "收盘"},
            "actions": [{"skill": "realtime_data", "tool": "get_stock_watchlist", "params": {}}],
        })
        assert "已创建" in result
        match = mgr.check_keyword("收盘")
        assert match is not None
        actions, name = match
        assert actions[0]["tool"] == "get_stock_watchlist"

        Path(rules_path).unlink(missing_ok=True)


class TestMetadataTracking:
    def test_metadata_lifecycle(self, tmp_path):
        learned_dir = tmp_path / "skills" / "learned"
        learned_dir.mkdir(parents=True)
        loader = SkillLoader(str(learned_dir))

        (learned_dir / "flight_skill.py").write_text(_VALID_SKILL)
        loader.update_metadata("flight_skill", {
            "taught_by": "allen", "description": "查航班", "enabled": True,
        })

        meta = loader.get_metadata("flight_skill")
        assert meta["taught_by"] == "allen"

        loader.update_metadata("flight_skill", {"enabled": False})
        assert loader.scan() == []

        loader.update_metadata("flight_skill", {"enabled": True})
        assert len(loader.scan()) == 1

        loader.remove_skill("flight_skill")
        assert not (learned_dir / "flight_skill.py").exists()
        assert loader.get_metadata("flight_skill") == {}
