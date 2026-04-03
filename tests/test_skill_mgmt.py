"""Tests for skills.skill_mgmt — skill management via voice."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skills.skill_mgmt import SkillManagementSkill


@pytest.fixture()
def skill():
    loader = MagicMock()
    loader.get_metadata.return_value = {}
    registry = MagicMock()
    registry.skill_names = ["weather", "time", "hello"]
    return SkillManagementSkill(loader, registry)


class TestListSkills:
    def test_list_all(self, skill):
        result = skill.execute("list_skills", {})
        assert "weather" in result
        assert "time" in result

    def test_list_separates_learned(self, skill):
        skill._loader.get_metadata.side_effect = lambda n: {"taught_by": "allen"} if n == "hello" else {}
        result = skill.execute("list_skills", {})
        assert "学会" in result or "hello" in result


class TestDisableSkill:
    def test_disable_learned(self, skill):
        skill._loader.get_metadata.return_value = {"taught_by": "allen"}
        result = skill.execute("disable_skill", {"skill_name": "hello"})
        assert "禁用" in result
        skill._loader.update_metadata.assert_called_once_with("hello", {"enabled": False})

    def test_delete_learned(self, skill):
        skill._loader.get_metadata.return_value = {"taught_by": "allen"}
        result = skill.execute("disable_skill", {"skill_name": "hello", "delete": True})
        assert "删除" in result
        skill._loader.remove_skill.assert_called_once_with("hello")

    def test_reject_builtin(self, skill):
        skill._loader.get_metadata.return_value = {}
        result = skill.execute("disable_skill", {"skill_name": "weather"})
        assert "内置" in result
