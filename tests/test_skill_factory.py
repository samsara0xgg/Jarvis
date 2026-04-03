"""Tests for core.skill_factory — Claude Code skill generation + security scan."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.skill_factory import SkillFactory


@pytest.fixture()
def factory(tmp_path):
    (tmp_path / "skills" / "learned").mkdir(parents=True)
    (tmp_path / "skills" / "__init__.py").write_text('class Skill:\n    pass\n')
    (tmp_path / "skills" / "weather.py").write_text('class WeatherSkill:\n    pass\n')
    return SkillFactory(
        learned_dir=str(tmp_path / "skills" / "learned"),
        project_root=str(tmp_path),
    )


class TestBuildPrompt:
    def test_contains_description(self, factory):
        prompt = factory._build_prompt("查航班信息", "class Skill: ...", "class WeatherSkill: ...")
        assert "查航班" in prompt
        assert "Skill" in prompt

    def test_contains_constraints(self, factory):
        prompt = factory._build_prompt("查汇率", "class Skill: ...", "class Ex: ...")
        assert "os.system" in prompt or "subprocess" in prompt


class TestSecurityScan:
    def test_clean_file_passes(self, factory, tmp_path):
        path = tmp_path / "clean.py"
        path.write_text("import requests\ndef fetch(): return requests.get('https://example.com')\n")
        assert factory._security_scan(str(path)) == []

    def test_os_system_blocked(self, factory, tmp_path):
        path = tmp_path / "evil.py"
        path.write_text('import os\nos.system("rm -rf /")\n')
        errors = factory._security_scan(str(path))
        assert len(errors) > 0
        assert any("os.system" in e for e in errors)

    def test_subprocess_blocked(self, factory, tmp_path):
        path = tmp_path / "sub.py"
        path.write_text('import subprocess\nsubprocess.run(["ls"])\n')
        errors = factory._security_scan(str(path))
        assert any("subprocess" in e for e in errors)

    def test_eval_blocked(self, factory, tmp_path):
        path = tmp_path / "ev.py"
        path.write_text('result = eval("1+1")\n')
        errors = factory._security_scan(str(path))
        assert any("eval" in e for e in errors)

    def test_exec_blocked(self, factory, tmp_path):
        path = tmp_path / "ex.py"
        path.write_text('exec("print(1)")\n')
        errors = factory._security_scan(str(path))
        assert any("exec" in e for e in errors)

    def test_importlib_blocked(self, factory, tmp_path):
        path = tmp_path / "imp.py"
        path.write_text('import importlib\nimportlib.import_module("subprocess")\n')
        errors = factory._security_scan(str(path))
        assert any("importlib" in e for e in errors)

    def test_ctypes_blocked(self, factory, tmp_path):
        path = tmp_path / "ct.py"
        path.write_text('import ctypes\n')
        errors = factory._security_scan(str(path))
        assert any("ctypes" in e for e in errors)

    def test_getattr_evasion_blocked(self, factory, tmp_path):
        path = tmp_path / "ga.py"
        path.write_text("getattr(os, 'system')('ls')\n")
        errors = factory._security_scan(str(path))
        assert len(errors) > 0


class TestSlugify:
    def test_chinese_removed(self, factory):
        slug = factory._slugify("查航班信息")
        assert slug
        assert slug.isascii()

    def test_spaces_to_underscores(self, factory):
        slug = factory._slugify("check flight info")
        assert slug == "check_flight_info"

    def test_chinese_produces_unique_slugs(self, factory):
        s1 = factory._slugify("查航班信息")
        s2 = factory._slugify("查汇率")
        assert s1 != s2  # different inputs → different slugs
        assert s1.isascii()
        assert s2.isascii()


class TestCreateNoCLI:
    def test_returns_failure_without_cli(self, factory, monkeypatch):
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = factory.create("查航班")
        assert result["success"] is False
        assert "未安装" in result["message"]
