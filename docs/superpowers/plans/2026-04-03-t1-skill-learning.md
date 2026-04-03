# T1: 技能学习 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让小贾能通过对话学习新技能——配置现有 skill 快捷方式、组合多个 skill、或调用 Claude Code 创造全新 skill。

**Architecture:** 三种学习模式共用一个学习意图检测层（`LearningRouter`），分流到不同处理器。配置型和组合型扩展现有 `AutomationRuleManager`（新增 `skill_alias` 触发类型），创造型通过 subprocess 调用 Claude Code CLI 生成 skill 文件。所有 learned skills 放在 `skills/learned/` 目录，由 `SkillLoader` 在启动时扫描加载。

**Tech Stack:** Python 3.11 / SQLite / Claude Code CLI (`claude` v2.1+) / importlib / ruff / pytest

**Design Spec:** `docs/superpowers/specs/2026-04-03-jarvis-growth-roadmap-design.md` T1 节

---

## Scope Check

T1 涵盖 4 个子系统：学习意图检测、配置型/组合型规则扩展、Claude Code 技能工厂、技能加载管理。这些子系统共享 `AutomationRuleManager` 和 `SkillRegistry` 接口，适合在一个 plan 中实现。

---

## File Map

| 操作 | 文件 | 职责 |
|------|------|------|
| Create | `core/learning_router.py` | 学习意图检测：关键词匹配 → 分类为 config/compose/create |
| Create | `core/skill_loader.py` | learned skills 扫描、importlib 加载、metadata 管理 |
| Create | `core/skill_factory.py` | Claude Code CLI 调用、安全扫描、测试验证 |
| Create | `skills/learned/__init__.py` | learned skills Python 包（空文件） |
| Create | `skills/skill_mgmt.py` | 技能管理 skill：list_skills / disable_skill |
| Modify | `core/automation_rules.py:153-177` | `check_keyword` 兼容 `skill_alias` 触发类型 |
| Modify | `core/local_executor.py` | 新增 `execute_skill_alias` 方法 |
| Modify | `jarvis.py:145-175` | 初始化学习组件；`_register_skills` 加载 learned skills |
| Modify | `jarvis.py:508-522` | keyword 检查区分 skill_alias vs smart_home |
| Modify | `jarvis.py:436` | 学习意图检测插入 pipeline |
| Create | `tests/test_learning_router.py` | 学习意图检测测试 |
| Create | `tests/test_skill_loader.py` | 热加载测试 |
| Create | `tests/test_skill_factory.py` | 技能工厂测试（安全扫描、prompt 构建） |
| Modify | `tests/test_automation_rules.py` | skill_alias 规则测试 |

---

## Task 1: LearningRouter — 学习意图检测

**Files:**
- Create: `core/learning_router.py`
- Test: `tests/test_learning_router.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_learning_router.py
"""Tests for core.learning_router — detect and classify learning intent."""

from __future__ import annotations

import pytest

from core.learning_router import LearningRouter, LearningIntent


@pytest.fixture()
def router():
    return LearningRouter(skill_names=["weather", "realtime_data", "time", "todos"])


class TestDetectNone:
    """Non-learning utterances should return None."""

    def test_normal_question(self, router):
        assert router.detect("今天天气怎么样") is None

    def test_command(self, router):
        assert router.detect("开客厅灯") is None

    def test_greeting(self, router):
        assert router.detect("你好") is None


class TestDetectConfig:
    """Config-type: user creates a shortcut for existing skill."""

    def test_alias_with_explicit_trigger(self, router):
        result = router.detect("以后我说收盘就帮我查 NVDA 和 AAPL")
        assert result is not None
        assert result.mode == "config"
        assert "收盘" in result.trigger

    def test_alias_morning(self, router):
        result = router.detect("以后说早安就帮我查天气")
        assert result is not None
        assert result.mode == "config"
        assert "早安" in result.trigger

    def test_remember_every_time(self, router):
        result = router.detect("记住每次说开工就打开VS Code")
        assert result is not None
        assert result.mode == "config"


class TestDetectCompose:
    """Compose-type: schedule + multiple skills."""

    def test_daily_multi_skill(self, router):
        result = router.detect("每天早上8点帮我查天气和股票")
        assert result is not None
        assert result.mode == "compose"

    def test_weekly_reminder(self, router):
        result = router.detect("每周一早上提醒我开周会")
        assert result is not None
        assert result.mode == "compose"

    def test_daily_single_skill(self, router):
        result = router.detect("每天晚上10点帮我查一下新闻")
        assert result is not None
        assert result.mode == "compose"


class TestDetectCreate:
    """Create-type: needs new code (Claude Code)."""

    def test_learn_new_skill(self, router):
        result = router.detect("学会查航班信息")
        assert result is not None
        assert result.mode == "create"
        assert "航班" in result.description

    def test_add_skill(self, router):
        result = router.detect("帮我加一个查快递的技能")
        assert result is not None
        assert result.mode == "create"

    def test_learn_with_prefix(self, router):
        result = router.detect("学一下帮我查汇率")
        assert result is not None
        assert result.mode == "create"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_learning_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.learning_router'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/learning_router.py
"""学习意图检测 — 判断用户是否在教小贾新技能。

分类为三种模式：
- config: 给现有 skill 设快捷方式（"以后说xxx就查xxx"）
- compose: 串联多个 skill + 定时（"每天8点查天气和股票"）
- create: 需要新代码（"学会查航班"）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

# 创造型关键词（优先检测，因为"学会"是最明确的信号）
_CREATE_KEYWORDS = ["学会", "学一下", "去学", "帮我加一个", "新增一个", "添加一个"]

# 配置型模式："以后(我说)X就(帮我)Y"
_CONFIG_PATTERNS = [
    re.compile(r"(?:以后|以后我说|以后说)(?:我说)?[「「]?(.+?)[」」]?(?:就|就帮我|你就|就给我|帮我)"),
    re.compile(r"记住每次(?:我说|说)?[「「]?(.+?)[」」]?(?:就|就帮我)"),
]

# 组合型关键词：定时触发
_SCHEDULE_KEYWORDS = ["每天", "每周", "每个月", "每隔", "定时"]


@dataclass
class LearningIntent:
    """学习意图检测结果。"""

    mode: str  # "config" | "compose" | "create"
    trigger: str = ""  # 配置型的触发词
    description: str = ""  # 用户原始描述
    raw_text: str = ""  # 原始输入


class LearningRouter:
    """检测和分类学习意图。

    Args:
        skill_names: 当前已注册的 skill 名称列表。
    """

    def __init__(self, skill_names: list[str] | None = None) -> None:
        self._skill_names = set(skill_names or [])

    def update_skills(self, skill_names: list[str]) -> None:
        """更新已注册 skill 列表。"""
        self._skill_names = set(skill_names)

    def detect(self, text: str) -> LearningIntent | None:
        """检测文本中的学习意图。

        Returns:
            LearningIntent if detected, None otherwise.
        """
        text = text.strip()

        # 1. 创造型：明确要学新能力（优先，最明确的信号）
        for kw in _CREATE_KEYWORDS:
            if kw in text:
                desc = text.split(kw, 1)[-1].strip()
                return LearningIntent(
                    mode="create", description=desc, raw_text=text,
                )

        # 2. 配置型："以后说X就Y"
        for pattern in _CONFIG_PATTERNS:
            match = pattern.search(text)
            if match:
                trigger = match.group(1).strip()
                return LearningIntent(
                    mode="config", trigger=trigger,
                    description=text, raw_text=text,
                )

        # 3. 组合型：有定时关键词
        has_schedule = any(kw in text for kw in _SCHEDULE_KEYWORDS)
        if has_schedule:
            return LearningIntent(
                mode="compose", description=text, raw_text=text,
            )

        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_learning_router.py -v`
Expected: all PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: no new failures

- [ ] **Step 6: Commit**

```bash
git add core/learning_router.py tests/test_learning_router.py
git commit -m "Add LearningRouter: detect and classify learning intent"
```

---

## Task 2: AutomationRules 扩展 skill_alias + LocalExecutor

扩展现有规则系统支持 `skill_alias` 触发类型，并在 `LocalExecutor` 中加 skill 执行方法。

**Files:**
- Modify: `core/automation_rules.py:153-177`
- Modify: `core/local_executor.py`
- Modify: `tests/test_automation_rules.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_automation_rules.py` 文件末尾追加：

```python
# --- Skill Alias ---

class TestSkillAlias:
    def test_create_skill_alias(self, manager):
        mgr, _ = manager
        result = mgr.create_rule({
            "name": "收盘快捷",
            "trigger": {"type": "skill_alias", "keyword": "收盘"},
            "actions": [{"skill": "realtime_data", "tool": "get_stock_watchlist",
                         "params": {"symbols": ["NVDA", "AAPL"]}}],
        })
        assert "已创建" in result

    def test_skill_alias_keyword_match(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "收盘快捷",
            "trigger": {"type": "skill_alias", "keyword": "收盘"},
            "actions": [{"skill": "realtime_data", "tool": "get_stock_watchlist",
                         "params": {"symbols": ["NVDA", "AAPL"]}}],
        })
        match = mgr.check_keyword("收盘")
        assert match is not None
        actions, name = match
        assert name == "收盘快捷"
        assert actions[0]["skill"] == "realtime_data"
        assert actions[0]["tool"] == "get_stock_watchlist"

    def test_skill_alias_no_false_match(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "收盘快捷",
            "trigger": {"type": "skill_alias", "keyword": "收盘"},
            "actions": [{"skill": "realtime_data", "tool": "get_stock_watchlist",
                         "params": {}}],
        })
        assert mgr.check_keyword("今天天气") is None

    def test_skill_alias_persists(self, tmp_rules_path, mock_scheduler):
        mgr1 = AutomationRuleManager(
            rules_path=tmp_rules_path, scheduler=mock_scheduler,
        )
        mgr1.create_rule({
            "name": "收盘快捷",
            "trigger": {"type": "skill_alias", "keyword": "收盘"},
            "actions": [{"skill": "realtime_data", "tool": "get_stock_watchlist",
                         "params": {}}],
        })
        mgr2 = AutomationRuleManager(
            rules_path=tmp_rules_path, scheduler=mock_scheduler,
        )
        assert mgr2.check_keyword("收盘") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_automation_rules.py::TestSkillAlias -v`
Expected: FAIL (skill_alias not matched by check_keyword)

- [ ] **Step 3: Modify `check_keyword` to accept skill_alias type**

In `core/automation_rules.py`, change the `check_keyword` method. Currently line 165 checks `if rule.trigger.get("type") != "keyword"`. Change it to:

```python
            trigger_type = rule.trigger.get("type")
            if trigger_type not in ("keyword", "skill_alias"):
                continue
```

This is a one-line change in the existing condition.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_automation_rules.py -v`
Expected: all PASS including new TestSkillAlias tests

- [ ] **Step 5: Add `execute_skill_alias` to LocalExecutor**

Append to `core/local_executor.py` before the closing of the class (after `execute_automation`):

```python
    def execute_skill_alias(
        self, actions: list[dict], user_role: str = "owner",
    ) -> ActionResponse:
        """执行 skill_alias actions — 调用指定 skill 的指定 tool.

        Args:
            actions: 包含 skill/tool/params 的 action 列表。
            user_role: 用户角色。

        Returns:
            ActionResponse — REQLLM，让 LLM 用小贾语气转述结果。
        """
        results = []
        for act in actions:
            tool_name = act.get("tool", "")
            params = act.get("params", {})
            if not tool_name:
                continue
            result = self.skill_registry.execute(
                tool_name, params, user_role=user_role,
            )
            self.logger.info("Skill alias execute: %s(%s) → %s", tool_name, params, result[:80])
            results.append(result)

        if not results:
            return ActionResponse(Action.RESPONSE, "没有需要执行的操作。")

        return ActionResponse(Action.REQLLM, "\n".join(results))
```

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: no new failures

- [ ] **Step 7: Commit**

```bash
git add core/automation_rules.py core/local_executor.py tests/test_automation_rules.py
git commit -m "Add skill_alias rule type and execute_skill_alias in LocalExecutor"
```

---

## Task 3: SkillLoader — learned skills 热加载

扫描 `skills/learned/` 目录，用 importlib 加载 Skill 子类，管理 metadata。

**Files:**
- Create: `core/skill_loader.py`
- Create: `skills/learned/__init__.py`
- Test: `tests/test_skill_loader.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_loader.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_loader.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Write implementation**

```python
# core/skill_loader.py
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
        """Scan directory and load all valid Skill subclasses.

        Returns:
            List of instantiated Skill objects. Invalid files are skipped with a warning.
        """
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
        """Load a single .py file and return the first Skill subclass instance."""
        module_name = f"skills.learned.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, Skill)
                and attr is not Skill
            ):
                return attr()
        return None

    def _load_metadata(self) -> dict[str, Any]:
        try:
            return json.loads(self._meta_path.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save_metadata(self, data: dict[str, Any]) -> None:
        self._meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def get_metadata(self, skill_id: str) -> dict[str, Any]:
        """Get metadata for a skill. Returns empty dict if not found."""
        return self._load_metadata().get(skill_id, {})

    def update_metadata(self, skill_id: str, updates: dict[str, Any]) -> None:
        """Merge updates into a skill's metadata."""
        data = self._load_metadata()
        if skill_id not in data:
            data[skill_id] = {}
        data[skill_id].update(updates)
        self._save_metadata(data)

    def remove_skill(self, skill_id: str) -> bool:
        """Remove a learned skill file and its metadata."""
        path = self._dir / f"{skill_id}.py"
        if path.exists():
            path.unlink()
        data = self._load_metadata()
        data.pop(skill_id, None)
        self._save_metadata(data)
        LOGGER.info("Removed learned skill: %s", skill_id)
        return True
```

- [ ] **Step 4: Create `skills/learned/__init__.py`**

```bash
mkdir -p skills/learned
touch skills/learned/__init__.py
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_skill_loader.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add core/skill_loader.py skills/learned/__init__.py tests/test_skill_loader.py
git commit -m "Add SkillLoader: scan, load, and manage learned skills"
```

---

## Task 4: SkillFactory — Claude Code 技能工厂

调用 Claude Code CLI 生成新 skill 文件，经过安全扫描和测试验证。

**Files:**
- Create: `core/skill_factory.py`
- Test: `tests/test_skill_factory.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_factory.py
"""Tests for core.skill_factory — Claude Code skill generation + security scan."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.skill_factory import SkillFactory


@pytest.fixture()
def factory(tmp_path):
    (tmp_path / "skills" / "learned").mkdir(parents=True)
    (tmp_path / "skills" / "__init__.py").write_text(
        'class Skill:\n    pass\n'
    )
    (tmp_path / "skills" / "weather.py").write_text(
        'class WeatherSkill:\n    pass\n'
    )
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
        assert "os.system" in prompt or "subprocess" in prompt  # security constraints mentioned


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


class TestSlugify:
    def test_chinese_removed(self, factory):
        slug = factory._slugify("查航班信息")
        assert slug  # non-empty
        # All chars should be ascii
        assert slug.isascii()

    def test_spaces_to_underscores(self, factory):
        slug = factory._slugify("check flight info")
        assert slug == "check_flight_info"


class TestCreateNoCLI:
    """Test create() when claude CLI is not available."""

    def test_returns_failure_without_cli(self, factory, monkeypatch):
        # Make subprocess.run raise FileNotFoundError
        import subprocess
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = factory.create("查航班")
        assert result["success"] is False
        assert "未安装" in result["message"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_factory.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Write implementation**

```python
# core/skill_factory.py
"""Claude Code 技能工厂 — 调用 CC CLI 生成新 skill 文件。

流程：准备上下文 → 调 claude CLI → 安全扫描 → pytest → 返回结果
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

# 安全黑名单
_DANGEROUS_PATTERNS = [
    (r"\bos\.system\b", "os.system"),
    (r"\bos\.popen\b", "os.popen"),
    (r"\bsubprocess\b", "subprocess module"),
    (r"\beval\s*\(", "eval()"),
    (r"\bexec\s*\(", "exec()"),
    (r"\b__import__\b", "__import__()"),
    (r"\bshutil\b", "shutil module"),
    (r"\bpickle\b", "pickle module"),
]


class SkillFactory:
    """Generate new skills by invoking Claude Code CLI.

    Args:
        learned_dir: Path to skills/learned/ directory.
        project_root: Path to the project root.
    """

    def __init__(
        self,
        learned_dir: str | Path = "skills/learned",
        project_root: str | Path = ".",
    ) -> None:
        self._dir = Path(learned_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._root = Path(project_root)

    def create(
        self,
        description: str,
        skill_name_hint: str = "",
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Generate a new skill from a natural language description.

        Args:
            description: What the skill should do (user's words).
            skill_name_hint: Optional suggested file name (without .py).
            on_status: Callback for progress updates.

        Returns:
            {"success": bool, "skill_name": str, "message": str, "path": str | None}
        """
        def status(msg: str) -> None:
            LOGGER.info("SkillFactory: %s", msg)
            if on_status:
                on_status(msg)

        # 1. Read Skill ABC and example skill
        status("准备上下文...")
        abc_source = self._read_file("skills/__init__.py")
        example_source = self._read_file("skills/weather.py")

        # 2. Build prompt
        prompt = self._build_prompt(description, abc_source, example_source)
        skill_id = skill_name_hint or self._slugify(description)
        skill_path = self._dir / f"{skill_id}.py"
        test_path = self._root / "tests" / f"test_learned_{skill_id}.py"

        # 3. Call Claude Code CLI
        status("正在学习...")
        try:
            result = subprocess.run(
                [
                    "claude", "-p", prompt,
                    "--allowedTools", "Edit,Write,Bash",
                    "--output-format", "text",
                ],
                capture_output=True, text=True, timeout=120,
                cwd=str(self._root),
            )
            if result.returncode != 0:
                return {
                    "success": False, "skill_name": skill_id,
                    "message": f"Claude Code 执行失败: {result.stderr[:200]}",
                    "path": None,
                }
        except FileNotFoundError:
            return {
                "success": False, "skill_name": skill_id,
                "message": "Claude Code CLI 未安装",
                "path": None,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False, "skill_name": skill_id,
                "message": "Claude Code 超时（120s）",
                "path": None,
            }

        # 4. Verify file was created
        if not skill_path.exists():
            return {
                "success": False, "skill_name": skill_id,
                "message": f"未生成 skill 文件: {skill_path.name}",
                "path": None,
            }

        # 5. Security scan
        status("安全检查...")
        security_errors = self._security_scan(str(skill_path))
        if security_errors:
            skill_path.unlink(missing_ok=True)
            return {
                "success": False, "skill_name": skill_id,
                "message": f"安全检查未通过: {'; '.join(security_errors)}",
                "path": None,
            }

        # 6. Run tests if generated
        if test_path.exists():
            status("运行测试...")
            try:
                test_result = subprocess.run(
                    ["python", "-m", "pytest", str(test_path), "-v", "--tb=short"],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(self._root),
                )
                if test_result.returncode != 0:
                    return {
                        "success": False, "skill_name": skill_id,
                        "message": f"测试未通过:\n{test_result.stdout[-300:]}",
                        "path": str(skill_path),
                    }
            except subprocess.TimeoutExpired:
                return {
                    "success": False, "skill_name": skill_id,
                    "message": "测试执行超时",
                    "path": str(skill_path),
                }

        status("学会了！")
        return {
            "success": True, "skill_name": skill_id,
            "message": "技能学习成功",
            "path": str(skill_path),
        }

    def _build_prompt(
        self, description: str, skill_abc_source: str, example_skill_source: str,
    ) -> str:
        """Build the Claude Code prompt for skill generation."""
        return f"""你需要为 Jarvis 语音助手写一个新的 skill。

## 需求
{description}

## Skill 接口（必须继承 Skill）
```python
{skill_abc_source}
```

## 范例 skill（参考格式和风格）
```python
{example_skill_source}
```

## 要求
1. 在 skills/learned/ 目录下创建一个 .py 文件，文件名用英文下划线命名
2. 继承 Skill ABC，实现 skill_name、get_tool_definitions、execute 三个方法
3. 在 tests/ 目录下创建对应的测试文件 test_learned_<name>.py
4. execute 方法接收 tool_name 和 tool_input，返回文本结果字符串
5. 网络请求用 requests 库，设置 timeout=10
6. 禁止使用 os.system、subprocess、eval、exec
7. 禁止读写 core/ 目录下的文件
8. 用 logging 模块，不用 print
9. 加 type hints
10. 只创建文件，不要输出其他说明文字"""

    def _security_scan(self, file_path: str) -> list[str]:
        """Scan a Python file for dangerous patterns. Returns list of violation descriptions."""
        content = Path(file_path).read_text()
        errors = []
        for pattern, desc in _DANGEROUS_PATTERNS:
            if re.search(pattern, content):
                errors.append(desc)
        return errors

    def _read_file(self, rel_path: str) -> str:
        """Read a file relative to project root."""
        path = self._root / rel_path
        if path.exists():
            return path.read_text()
        return f"# File not found: {rel_path}"

    def _slugify(self, text: str) -> str:
        """Convert a description to a valid Python module name."""
        slug = text[:30].strip().lower()
        slug = re.sub(r"[^\w\s]", "", slug)
        slug = re.sub(r"\s+", "_", slug)
        slug = re.sub(r"[^\x00-\x7f]", "", slug)  # remove non-ascii
        return slug.strip("_") or "custom_skill"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_skill_factory.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add core/skill_factory.py tests/test_skill_factory.py
git commit -m "Add SkillFactory: Claude Code skill generation with security scan"
```

---

## Task 5: SkillManagementSkill — 查询/禁用/删除

让用户通过对话管理已学技能（"你都会什么"、"忘掉查航班"）。

**Files:**
- Create: `skills/skill_mgmt.py`
- Test: `tests/test_skill_mgmt.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_mgmt.py
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
        # hello is "learned" (has metadata)
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


class TestRequiresUserId:
    def test_no_user_rejected(self, skill):
        result = skill.execute("list_skills", {}, user_id=None)
        # list_skills 不需要 user_id，应该正常返回
        assert "weather" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_mgmt.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Write implementation**

```python
# skills/skill_mgmt.py
"""Skill management — list, disable, remove learned skills via voice."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class SkillManagementSkill(Skill):
    """Allows users to query and manage learned skills.

    Args:
        skill_loader: SkillLoader instance for metadata and removal.
        skill_registry: SkillRegistry instance for listing.
    """

    def __init__(self, skill_loader: Any, skill_registry: Any) -> None:
        self._loader = skill_loader
        self._registry = skill_registry

    @property
    def skill_name(self) -> str:
        return "skill_mgmt"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_skills",
                "description": (
                    "List all skills Jarvis can use (built-in and learned). "
                    "Use when user asks 'what can you do' or 'what skills do you have'."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "disable_skill",
                "description": (
                    "Disable or permanently delete a learned skill. "
                    "Use when user says 'forget skill X' or 'remove skill X'. "
                    "Cannot disable built-in skills."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the learned skill to disable or delete.",
                        },
                        "delete": {
                            "type": "boolean",
                            "description": "True to permanently delete file, False to just disable. Default False.",
                        },
                    },
                    "required": ["skill_name"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **ctx: Any) -> str:
        if tool_name == "list_skills":
            return self._list_skills()
        if tool_name == "disable_skill":
            return self._disable_skill(
                tool_input.get("skill_name", ""),
                tool_input.get("delete", False),
            )
        return f"Unknown tool: {tool_name}"

    def _list_skills(self) -> str:
        names = self._registry.skill_names
        builtin = [n for n in names if not self._is_learned(n)]
        learned = [n for n in names if self._is_learned(n)]
        parts = [f"内置技能（{len(builtin)}个）：{', '.join(builtin)}"]
        if learned:
            parts.append(f"学会的技能（{len(learned)}个）：{', '.join(learned)}")
        else:
            parts.append("还没学会新技能。")
        return "\n".join(parts)

    def _disable_skill(self, name: str, delete: bool) -> str:
        if not self._is_learned(name):
            return f"'{name}' 是内置技能，不能删除。"
        if delete:
            self._loader.remove_skill(name)
            return f"已永久删除技能 '{name}'。"
        self._loader.update_metadata(name, {"enabled": False})
        return f"已禁用技能 '{name}'，重启后生效。"

    def _is_learned(self, name: str) -> bool:
        return bool(self._loader.get_metadata(name))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_skill_mgmt.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add skills/skill_mgmt.py tests/test_skill_mgmt.py
git commit -m "Add SkillManagementSkill: list, disable, remove learned skills"
```

---

## Task 6: Wire everything into jarvis.py

初始化所有 T1 组件，接入 utterance pipeline。

**Files:**
- Modify: `jarvis.py`

- [ ] **Step 1: Add imports and initialization in `__init__`**

After the `self.skill_loader` initialization (in `_register_skills`), add learning components. In `_register_skills` method, after all existing skill registrations, add:

```python
        # --- Load learned skills ---
        from core.skill_loader import SkillLoader
        self.skill_loader = SkillLoader("skills/learned")
        for skill in self.skill_loader.scan():
            try:
                self.skill_registry.register(skill)
            except Exception as exc:
                self.logger.warning("Failed to register learned skill %s: %s", skill.skill_name, exc)

        # --- Skill management ---
        from skills.skill_mgmt import SkillManagementSkill
        self.skill_registry.register(SkillManagementSkill(self.skill_loader, self.skill_registry))
```

After `_register_skills` completes (in `__init__`, around line 175), add:

```python
        # --- Learning router + skill factory ---
        from core.learning_router import LearningRouter
        from core.skill_factory import SkillFactory
        self.learning_router = LearningRouter(
            skill_names=list(self.skill_registry.skill_names),
        )
        self.skill_factory = SkillFactory(
            learned_dir="skills/learned",
            project_root=str(self.config_path.parent) if self.config_path else ".",
        )
```

- [ ] **Step 2: Add learning detection in `_handle_utterance_inner`**

After Level 1 direct answer (step 4b) and before keyword check (step 5), insert:

```python
        # 4c. Learning intent detection
        if hasattr(self, "learning_router"):
            learning = self.learning_router.detect(text)
            if learning:
                self.logger.info("Learning intent: mode=%s desc=%s", learning.mode, learning.description[:60])
                learn_response = self._handle_learning(learning, user_id, user_role)
                if learn_response:
                    self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                    print(f"🤖 Jarvis: {learn_response}")
                    self._speak_nonblocking(learn_response, emotion=detected_emotion)
                    # 存对话历史
                    history.append({"role": "user", "content": text})
                    history.append({"role": "assistant", "content": learn_response})
                    self.conversation_store.replace(session_id, history)
                    if hasattr(self, "behavior_log") and user_id:
                        self.behavior_log.log(user_id, "conversation", {
                            "text": text[:100], "route": f"learn_{learning.mode}",
                        })
                    return learn_response
```

- [ ] **Step 3: Add keyword check for skill_alias in step 5**

Modify the existing keyword trigger check to distinguish skill_alias from smart_home:

```python
        # 5. Keyword trigger check (before routing)
        self.event_bus.emit("jarvis.state_changed", {"state": "thinking"})
        response_text = None
        updated_messages = None
        ar: ActionResponse | None = None
        sentence_count = 0

        if self.rule_manager and self.local_executor:
            match = self.rule_manager.check_keyword(text)
            if match:
                keyword_actions, rule_name = match
                if keyword_actions and keyword_actions[0].get("skill"):
                    # skill_alias: 调 skill 然后让 LLM 转述
                    ar = self.local_executor.execute_skill_alias(
                        keyword_actions, user_role,
                    )
                    if ar.action == Action.REQLLM:
                        use_llm_rephrase = True
                    else:
                        response_text = ar.text
                else:
                    ar = self.local_executor.execute_smart_home(
                        keyword_actions, user_role, response=f"好的，{rule_name}已执行。",
                    )
                    response_text = ar.text
```

- [ ] **Step 4: Implement `_handle_learning` method**

Add to `JarvisApp` class:

```python
    def _handle_learning(
        self, intent: "LearningIntent", user_id: str | None, user_role: str,
    ) -> str | None:
        """Handle a detected learning intent.

        Config and compose modes return None to fall through to cloud LLM
        (which will use the automation skill to create rules).
        Create mode invokes the skill factory synchronously.
        """
        if intent.mode == "create":
            return self._learn_create(intent, user_id)
        # config and compose: let cloud LLM handle via existing automation skill
        # The LLM already knows how to create rules via the intent router
        return None

    def _learn_create(self, intent: "LearningIntent", user_id: str | None) -> str:
        """创造型：调用 Claude Code 技能工厂。"""
        self.speak("好的，我去学一下，稍等。")

        result = self.skill_factory.create(
            description=intent.description,
            on_status=lambda msg: self.logger.info("SkillFactory: %s", msg),
        )

        if result["success"]:
            # 热加载新 skill
            try:
                new_skills = self.skill_loader.scan()
                for skill in new_skills:
                    if skill.skill_name not in self.skill_registry.skill_names:
                        self.skill_registry.register(skill)
                        self.skill_loader.update_metadata(skill.skill_name, {
                            "taught_by": user_id or "unknown",
                            "description": intent.description,
                        })
                self.learning_router.update_skills(list(self.skill_registry.skill_names))
            except Exception as exc:
                self.logger.warning("Failed to hot-load new skill: %s", exc)
                return f"技能文件生成了但加载失败：{exc}"

            if user_id and hasattr(self, "behavior_log"):
                self.behavior_log.log(user_id, "skill_learned", {
                    "skill": result["skill_name"],
                    "description": intent.description,
                })
            return f"学会了！现在我可以{intent.description}了，要试试吗？"
        else:
            return f"没学会，{result['message']}"
```

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: no new failures

- [ ] **Step 6: Commit**

```bash
git add jarvis.py
git commit -m "Wire LearningRouter, SkillFactory, SkillLoader, SkillMgmt into jarvis pipeline"
```

---

## Task 7: End-to-end tests + final verification

**Files:**
- Create: `tests/test_learning_e2e.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_learning_e2e.py
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
    """Test the flow: detect intent → classify → loader can load result."""

    def test_create_detected_then_loaded(self, tmp_path):
        # 1. Detect create intent
        router = LearningRouter(skill_names=["weather", "time"])
        intent = router.detect("学会查航班信息")
        assert intent is not None
        assert intent.mode == "create"

        # 2. Simulate: factory created the file
        learned_dir = tmp_path / "skills" / "learned"
        learned_dir.mkdir(parents=True)
        (learned_dir / "flight_skill.py").write_text(_VALID_SKILL)

        # 3. Loader scans and loads
        loader = SkillLoader(str(learned_dir))
        skills = loader.scan()
        assert len(skills) == 1
        assert skills[0].skill_name == "flight"

        # 4. Skill actually works
        result = skills[0].execute("check_flight", {"route": "YVR-PEK"})
        assert "YVR-PEK" in result


class TestSecurityScanIntegration:
    """Test that security scan catches dangerous patterns in factory."""

    def test_factory_rejects_dangerous_code(self, tmp_path):
        (tmp_path / "skills" / "learned").mkdir(parents=True)
        factory = SkillFactory(
            learned_dir=str(tmp_path / "skills" / "learned"),
            project_root=str(tmp_path),
        )
        # Write a dangerous file directly
        evil_path = tmp_path / "skills" / "learned" / "evil.py"
        evil_path.write_text('import os; os.system("rm -rf /")')
        errors = factory._security_scan(str(evil_path))
        assert len(errors) > 0


class TestSkillAliasFlow:
    """Test config-type learning: keyword → skill alias rule."""

    def test_detect_config_then_create_rule(self):
        from core.automation_rules import AutomationRuleManager

        router = LearningRouter(skill_names=["realtime_data"])
        intent = router.detect("以后我说收盘就帮我查股票")
        assert intent is not None
        assert intent.mode == "config"
        assert "收盘" in intent.trigger

        # Create the rule (in practice, LLM would parse the full action)
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

        # Verify it triggers
        match = mgr.check_keyword("收盘")
        assert match is not None
        actions, name = match
        assert actions[0]["tool"] == "get_stock_watchlist"

        Path(rules_path).unlink(missing_ok=True)


class TestMetadataTracking:
    """Test that skill metadata is properly tracked."""

    def test_metadata_lifecycle(self, tmp_path):
        learned_dir = tmp_path / "skills" / "learned"
        learned_dir.mkdir(parents=True)
        loader = SkillLoader(str(learned_dir))

        # Create
        (learned_dir / "flight_skill.py").write_text(_VALID_SKILL)
        loader.update_metadata("flight_skill", {
            "taught_by": "allen",
            "description": "查航班",
            "enabled": True,
        })

        # Read
        meta = loader.get_metadata("flight_skill")
        assert meta["taught_by"] == "allen"

        # Disable
        loader.update_metadata("flight_skill", {"enabled": False})
        assert loader.scan() == []  # disabled, not loaded

        # Re-enable
        loader.update_metadata("flight_skill", {"enabled": True})
        skills = loader.scan()
        assert len(skills) == 1

        # Remove
        loader.remove_skill("flight_skill")
        assert not (learned_dir / "flight_skill.py").exists()
        assert loader.get_metadata("flight_skill") == {}
```

- [ ] **Step 2: Run E2E tests**

Run: `python -m pytest tests/test_learning_e2e.py -v`
Expected: all PASS

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: no new failures

- [ ] **Step 4: Ruff check**

Run: `ruff check core/learning_router.py core/skill_factory.py core/skill_loader.py skills/skill_mgmt.py`
Expected: no errors (or only pre-existing)

- [ ] **Step 5: Commit**

```bash
git add tests/test_learning_e2e.py
git commit -m "Add T1 end-to-end integration tests"
```

- [ ] **Step 6: Final verification commit (if any leftover files)**

Run: `git status`
If clean: T1 complete.
