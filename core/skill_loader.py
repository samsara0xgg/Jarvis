"""Dynamic skill loader — scan, load, and manage learned skills.

Scans skills/learned/ for Python files containing Skill subclasses,
loads them via importlib, and manages per-skill metadata in _metadata.json.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class SkillLoader:
    """Scan and load skill files from a directory.

    Args:
        learned_dir: Path to the skills/learned/ directory.
    """

    def __init__(self, learned_dir: str | Path = "skills/learned") -> None:
        self._dir = Path(learned_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self._dir / "_metadata.json"
        if not self._meta_path.exists():
            self._meta_path.write_text("{}")
        init_path = self._dir / "__init__.py"
        if not init_path.exists():
            init_path.write_text("")

    def scan(self) -> list[Skill]:
        """Scan directory and load all valid Skill subclasses."""
        skills: list[Skill] = []
        metadata = self._load_metadata()

        for py_file in sorted(self._dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            skill_id = py_file.stem
            meta = metadata.get(skill_id, {})
            if not meta.get("enabled", True):
                LOGGER.info("Skipping disabled learned skill: %s", skill_id)
                continue
            try:
                skill = self._load_file(py_file)
                if skill:
                    skills.append(skill)
                    LOGGER.info("Loaded learned skill: %s from %s", skill.skill_name, py_file.name)
            except Exception:
                LOGGER.exception("Failed to load skill from %s", py_file.name)

        return skills

    def _load_file(self, path: Path) -> Skill | None:
        module_name = f"skills.learned.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, Skill) and attr is not Skill:
                # 尝试无参实例化，失败则传空 config
                try:
                    return attr()
                except TypeError:
                    try:
                        return attr({})
                    except Exception:
                        LOGGER.warning("Cannot instantiate %s", attr.__name__)
                        return None
        return None

    def _load_metadata(self) -> dict[str, Any]:
        try:
            return json.loads(self._meta_path.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save_metadata(self, data: dict[str, Any]) -> None:
        """Atomic write: write to temp file then rename."""
        import os
        import tempfile
        content = json.dumps(data, indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._meta_path.parent), suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp_path, self._meta_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def get_metadata(self, skill_id: str) -> dict[str, Any]:
        return self._load_metadata().get(skill_id, {})

    def update_metadata(self, skill_id: str, updates: dict[str, Any]) -> None:
        data = self._load_metadata()
        if skill_id not in data:
            data[skill_id] = {}
        data[skill_id].update(updates)
        self._save_metadata(data)

    def remove_skill(self, skill_id: str) -> bool:
        path = self._dir / f"{skill_id}.py"
        if path.exists():
            path.unlink()
        data = self._load_metadata()
        data.pop(skill_id, None)
        self._save_metadata(data)
        LOGGER.info("Removed learned skill: %s", skill_id)
        return True
