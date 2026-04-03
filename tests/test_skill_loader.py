"""Tests for core.skill_loader — dynamic skill loading from skills/learned/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.skill_loader import SkillLoader

_VALID_SKILL = '''
from skills import Skill
from typing import Any

class HelloSkill(Skill):
    @property
    def skill_name(self) -> str:
        return "hello"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [{"name": "say_hello", "description": "Say hello",
                 "input_schema": {"type": "object", "properties": {}}}]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **ctx: Any) -> str:
        return "Hello!"
'''


@pytest.fixture()
def loader(tmp_path):
    learned_dir = tmp_path / "skills" / "learned"
    learned_dir.mkdir(parents=True)
    return SkillLoader(str(learned_dir))


class TestScan:
    def test_empty_dir(self, loader):
        assert loader.scan() == []

    def test_load_valid_skill(self, loader):
        (Path(loader._dir) / "hello_skill.py").write_text(_VALID_SKILL)
        skills = loader.scan()
        assert len(skills) == 1
        assert skills[0].skill_name == "hello"

    def test_skip_invalid_file(self, loader):
        (Path(loader._dir) / "bad_skill.py").write_text("def broken(")
        skills = loader.scan()
        assert skills == []

    def test_skip_underscore_files(self, loader):
        (Path(loader._dir) / "_internal.py").write_text(_VALID_SKILL)
        assert loader.scan() == []

    def test_skip_disabled_skill(self, loader):
        (Path(loader._dir) / "hello_skill.py").write_text(_VALID_SKILL)
        loader.update_metadata("hello_skill", {"enabled": False})
        assert loader.scan() == []


class TestMetadata:
    def test_read_write(self, loader):
        loader.update_metadata("hello", {"taught_by": "allen", "created": "2026-04-03"})
        meta = loader.get_metadata("hello")
        assert meta["taught_by"] == "allen"

    def test_update_preserves_existing(self, loader):
        loader.update_metadata("hello", {"taught_by": "allen"})
        loader.update_metadata("hello", {"uses": 5})
        meta = loader.get_metadata("hello")
        assert meta["taught_by"] == "allen"
        assert meta["uses"] == 5

    def test_missing_returns_empty(self, loader):
        assert loader.get_metadata("nonexistent") == {}


class TestRemove:
    def test_remove_skill(self, loader):
        skill_path = Path(loader._dir) / "hello_skill.py"
        skill_path.write_text(_VALID_SKILL)
        loader.update_metadata("hello_skill", {"taught_by": "allen"})
        loader.remove_skill("hello_skill")
        assert not skill_path.exists()
        assert loader.get_metadata("hello_skill") == {}
