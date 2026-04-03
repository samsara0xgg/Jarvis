# T1: 技能学习 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让小贾能通过对话学习新技能——配置现有 skill 快捷方式、组合多个 skill、或调用 Claude Code 创造全新 skill。

**Architecture:** 三种学习模式共用一个学习意图检测层，分流到不同处理器。配置型和组合型扩展现有 `AutomationRuleManager`，创造型通过 subprocess 调用 Claude Code CLI 生成 skill 文件。所有 learned skills 放在 `skills/learned/` 目录，启动时自动扫描加载。

**Tech Stack:** Python 3.11 / SQLite / Claude Code CLI / importlib / ruff / pytest

**Design Spec:** `docs/superpowers/specs/2026-04-03-jarvis-growth-roadmap-design.md` T1 节

---

## File Map

| 操作 | 文件 | 职责 |
|------|------|------|
| Create | `core/learning_router.py` | 学习意图检测 + 分流（配置/组合/创造） |
| Create | `core/skill_factory.py` | Claude Code 技能工厂（创造型） |
| Create | `core/skill_loader.py` | learned skills 热加载 + 启动扫描 |
| Create | `skills/learned/__init__.py` | learned skills 包 |
| Create | `skills/learned/_metadata.json` | learned skills 元数据（谁教的、何时、用了几次） |
| Modify | `core/automation_rules.py` | 新增 `skill_alias` 触发类型 + skill 列表 action |
| Modify | `core/local_executor.py` | 新增 `execute_skill_alias` 方法 |
| Modify | `jarvis.py` | 接入学习路由 + skill loader |
| Modify | `core/intent_router.py` | 路由 prompt 加 `learn` 意图 |
| Create | `tests/test_learning_router.py` | 学习意图检测测试 |
| Create | `tests/test_skill_factory.py` | 技能工厂测试 |
| Create | `tests/test_skill_loader.py` | 热加载测试 |
| Modify | `tests/test_automation_rules.py` | skill_alias 测试 |

---

## Task 1: 学习意图检测器

检测用户是否在"教"小贾，并分类为配置/组合/创造。

**Files:**
- Create: `core/learning_router.py`
- Create: `tests/test_learning_router.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_learning_router.py
"""Tests for core.learning_router — detect and classify learning intent."""

from __future__ import annotations

import pytest

from core.learning_router import LearningRouter, LearningIntent


class TestLearningRouter:
    @pytest.fixture()
    def router(self):
        # skill_names 模拟已注册的 skills
        return LearningRouter(skill_names=["weather", "realtime_data", "time", "todos"])

    def test_not_learning(self, router):
        result = router.detect("今天天气怎么样")
        assert result is None

    def test_config_alias(self, router):
        result = router.detect("以后我说收盘就帮我查 NVDA 和 AAPL")
        assert result is not None
        assert result.mode == "config"
        assert result.trigger == "收盘"

    def test_config_shortcut(self, router):
        result = router.detect("以后说早安就帮我查天气")
        assert result is not None
        assert result.mode == "config"

    def test_compose_cron(self, router):
        result = router.detect("每天早上8点帮我查天气和股票")
        assert result is not None
        assert result.mode == "compose"

    def test_create_new_skill(self, router):
        result = router.detect("学会查航班信息")
        assert result is not None
        assert result.mode == "create"

    def test_create_add_skill(self, router):
        result = router.detect("帮我加一个查快递的技能")
        assert result is not None
        assert result.mode == "create"

    def test_learning_keyword_patterns(self, router):
        """Various learning phrases should be detected."""
        phrases = [
            ("以后我说开工就打开VS Code", "config"),
            ("记住每次说晚安就关灯", "config"),
            ("学会帮我查汇率", "create"),
            ("每周一早上提醒我开周会", "compose"),
        ]
        for phrase, expected_mode in phrases:
            result = router.detect(phrase)
            assert result is not None, f"Failed to detect: {phrase}"
            assert result.mode == expected_mode, f"{phrase}: expected {expected_mode}, got {result.mode}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_learning_router.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 实现 LearningRouter**

```python
# core/learning_router.py
"""学习意图检测 — 判断用户是否在教小贾新技能，分类为配置/组合/创造。

配置型：给现有 skill 设快捷方式（"以后说xxx就查xxx"）
组合型：串联多个 skill + 定时（"每天8点查天气和股票"）
创造型：需要新代码（"学会查航班"）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger(__name__)

# 教学意图关键词
_LEARN_TRIGGERS = [
    "以后", "以后我说", "以后说",
    "记住每次", "记住以后",
    "学会", "学一下", "去学",
    "帮我加一个", "新增一个", "添加一个",
    "每天", "每周", "每个",  # 定时组合
]

# 配置型特征：有明确的 trigger → action 映射
_CONFIG_PATTERN = re.compile(
    r"(?:以后|以后我说|以后说|记住每次|记住以后)(?:我说)?[「「]?(.+?)[」」]?"
    r"(?:就|就帮我|你就|就给我|帮我)(.+)",
)

# 组合型特征：有定时 + 多个动作
_COMPOSE_KEYWORDS = ["每天", "每周", "每个月", "每隔", "定时"]
_COMPOSE_MULTI = re.compile(r"(?:和|跟|以及|还有|再|并且)")

# 创造型特征
_CREATE_KEYWORDS = ["学会", "学一下", "去学", "帮我加一个", "新增一个", "添加一个"]


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

        # 先检查是否包含任何教学意图关键词
        has_trigger = any(kw in text for kw in _LEARN_TRIGGERS)
        if not has_trigger:
            return None

        # 1. 创造型：明确要学新能力
        for kw in _CREATE_KEYWORDS:
            if kw in text:
                # 排除已有 skill 能处理的
                desc = text.split(kw, 1)[-1].strip()
                return LearningIntent(
                    mode="create", description=desc, raw_text=text,
                )

        # 2. 组合型：定时 + 多个动作
        has_schedule = any(kw in text for kw in _COMPOSE_KEYWORDS)
        has_multi = bool(_COMPOSE_MULTI.search(text))
        if has_schedule and has_multi:
            return LearningIntent(mode="compose", description=text, raw_text=text)

        # 3. 配置型：trigger → action 映射
        match = _CONFIG_PATTERN.search(text)
        if match:
            trigger = match.group(1).strip()
            return LearningIntent(
                mode="config", trigger=trigger, description=text, raw_text=text,
            )

        # 4. 有定时但只有一个动作 → 也是组合型（单 skill + cron）
        if has_schedule:
            return LearningIntent(mode="compose", description=text, raw_text=text)

        # 有教学关键词但无法分类 → 默认创造型
        return LearningIntent(mode="create", description=text, raw_text=text)
```

- [ ] **Step 4: 跑测试调优正则直到全部通过**

Run: `python -m pytest tests/test_learning_router.py -v`
Expected: 全部 PASS（可能需要微调正则）

- [ ] **Step 5: Commit**

```bash
git add core/learning_router.py tests/test_learning_router.py
git commit -m "Add LearningRouter: detect and classify learning intent"
```

---

## Task 2: 扩展 AutomationRuleManager 支持 skill_alias

在现有规则系统中加入 `skill_alias` 触发类型：trigger 是关键词，action 是调用指定 skill。

**Files:**
- Modify: `core/automation_rules.py`
- Modify: `core/local_executor.py`
- Modify: `tests/test_automation_rules.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_automation_rules.py — 新增
class TestSkillAlias:
    def test_create_skill_alias(self, rule_manager):
        result = rule_manager.create_rule({
            "name": "收盘快捷",
            "trigger": {"type": "skill_alias", "keyword": "收盘"},
            "actions": [{"skill": "realtime_data", "tool": "get_stock_watchlist", "params": {"symbols": ["NVDA", "AAPL"]}}],
        })
        assert "已创建" in result

    def test_check_skill_alias(self, rule_manager):
        rule_manager.create_rule({
            "name": "收盘快捷",
            "trigger": {"type": "skill_alias", "keyword": "收盘"},
            "actions": [{"skill": "realtime_data", "tool": "get_stock_watchlist", "params": {"symbols": ["NVDA", "AAPL"]}}],
        })
        match = rule_manager.check_keyword("收盘")
        assert match is not None
        actions, name = match
        assert name == "收盘快捷"
        assert actions[0]["skill"] == "realtime_data"
```

- [ ] **Step 2: 确认现有 `check_keyword` 已经兼容 `skill_alias`**

当前 `check_keyword` 检查 `trigger.type == "keyword"`。需要也接受 `"skill_alias"` 类型。

- [ ] **Step 3: 修改 `check_keyword` 兼容 skill_alias**

`core/automation_rules.py` — 修改 `check_keyword` 中的条件：

```python
    def check_keyword(self, text: str) -> tuple[list[dict[str, Any]], str] | None:
        text_stripped = text.strip()
        for rule in self._rules.values():
            if not rule.enabled:
                continue
            trigger_type = rule.trigger.get("type")
            if trigger_type not in ("keyword", "skill_alias"):
                continue
            keyword = rule.trigger.get("keyword", "")
            if not keyword:
                continue
            if text_stripped == keyword or text_stripped.startswith(keyword):
                self.logger.info("Keyword triggered: '%s' matched rule '%s'", text_stripped, rule.name)
                return rule.actions, rule.name
        return None
```

- [ ] **Step 4: 在 `local_executor.py` 加 `execute_skill_alias` 方法**

```python
    def execute_skill_alias(
        self, actions: list[dict], user_role: str = "owner",
    ) -> ActionResponse:
        """执行 skill_alias actions — 调用指定 skill 的指定 tool。"""
        results = []
        for act in actions:
            tool_name = act.get("tool", "")
            params = act.get("params", {})
            if not tool_name:
                continue
            result = self.skill_registry.execute(
                tool_name, params, user_role=user_role,
            )
            results.append(result)

        if not results:
            return ActionResponse(Action.RESPONSE, "没有需要执行的操作。")

        return ActionResponse(Action.REQLLM, "\n".join(results))
```

- [ ] **Step 5: 在 `jarvis.py` 的 keyword match 处区分 skill_alias**

在 `_handle_utterance_inner` step 5 keyword trigger 检查中，检查 actions 是否包含 `skill` 字段：

```python
        if self.rule_manager and self.local_executor:
            match = self.rule_manager.check_keyword(text)
            if match:
                keyword_actions, rule_name = match
                # 区分 skill_alias 和普通 keyword
                if keyword_actions and keyword_actions[0].get("skill"):
                    ar = self.local_executor.execute_skill_alias(
                        keyword_actions, user_role,
                    )
                    use_llm_rephrase = True
                    response_text = None  # 让 LLM 转述
                else:
                    ar = self.local_executor.execute_smart_home(
                        keyword_actions, user_role, response=f"好的，{rule_name}已执行。",
                    )
                    response_text = ar.text
```

- [ ] **Step 6: 跑测试**

Run: `python -m pytest tests/test_automation_rules.py tests/test_local_executor.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add core/automation_rules.py core/local_executor.py jarvis.py tests/test_automation_rules.py
git commit -m "Add skill_alias rule type: keyword triggers for existing skills"
```

---

## Task 3: 配置型学习处理器

用户说"以后说收盘就查 NVDA 和 AAPL"时，通过 LLM 解析出 trigger/skill/params，创建 skill_alias 规则。

**Files:**
- Modify: `core/learning_router.py`
- Modify: `jarvis.py`

- [ ] **Step 1: 在 LearningRouter 中加配置型处理方法**

```python
    def handle_config(self, intent: LearningIntent, llm_client: Any,
                      skill_names: list[str]) -> dict | None:
        """用 LLM 解析配置型学习意图，返回 rule_data。

        Returns:
            rule_data dict for AutomationRuleManager.create_rule(), or None if failed.
        """
        prompt = f"""用户想要创建一个快捷指令。分析以下请求，提取触发词和要执行的技能。

可用技能及对应工具：
{self._format_skill_info(skill_names)}

用户请求：{intent.raw_text}

返回 JSON（严格 JSON，无注释）：
{{
  "name": "简短规则名",
  "trigger_keyword": "触发词",
  "tool": "工具名",
  "params": {{}} 
}}

如果无法匹配到可用技能，返回 {{"error": "原因"}}"""
        # LLM 调用由调用方处理
        return prompt
```

实际的 LLM 调用放在 jarvis.py 的 pipeline 中，这里只生成 prompt。

- [ ] **Step 2: 在 jarvis.py 中接入学习路由**

在 `_handle_utterance_inner` 中，Level 1 之后、keyword 检查之前，加学习意图检测：

```python
        # 4c. Learning intent detection
        if hasattr(self, 'learning_router'):
            learning = self.learning_router.detect(text)
            if learning:
                self.logger.info("Learning intent: mode=%s desc=%s", learning.mode, learning.description)
                if learning.mode == "config":
                    # 配置型：LLM 解析 → 创建 skill_alias 规则
                    # （具体实现见 Task 3）
                    pass
                elif learning.mode == "compose":
                    # 组合型：LLM 解析 → 创建 cron + skill 列表规则
                    # （具体实现见 Task 4）
                    pass
                elif learning.mode == "create":
                    # 创造型：Claude Code 技能工厂
                    # （具体实现见 Task 5-6）
                    pass
```

配置型的完整处理逻辑：检测到 config → 调 LLM 解析 → 创建 rule → 回复用户。

- [ ] **Step 3: 测试端到端**
- [ ] **Step 4: Commit**

```bash
git commit -m "Add config learning handler: parse skill alias from natural language"
```

---

## Task 4: 组合型学习处理器

用户说"每天早上8点帮我查天气和股票"时，创建 cron 规则 + skill 列表。

**Files:**
- Modify: `core/automation_rules.py` — action 支持 skill 列表执行
- Modify: `core/learning_router.py` — 组合型处理方法
- Modify: `jarvis.py` — 接入组合型处理

- [ ] **Step 1: 扩展 `_execute_actions` 支持 skill 类型 action**

`core/automation_rules.py` — 在 `_execute_actions` 中，如果 action 包含 `skill` 字段，调用 skill_registry 而不是 smart_home：

```python
    def _execute_actions(self, actions: list[dict[str, Any]]) -> None:
        if self.action_executor:
            self.action_executor(actions)
        else:
            self.logger.warning("No action executor configured")
```

现有 `action_executor` 回调只处理 smart_home actions。需要扩展为也能处理 skill actions。最简单的方式是在 `jarvis.py` 的 `_execute_rule_actions` 回调中加判断。

- [ ] **Step 2: 在 LearningRouter 中加组合型处理**

类似配置型，生成 LLM prompt 解析用户请求，提取 cron 时间 + skill 列表。

- [ ] **Step 3: 在 jarvis.py 中实现组合型创建**
- [ ] **Step 4: 测试**
- [ ] **Step 5: Commit**

```bash
git commit -m "Add compose learning handler: cron + multi-skill rules"
```

---

## Task 5: Skill Loader — learned skills 热加载

`skills/learned/` 目录下的 skill 文件自动扫描、加载、注册。

**Files:**
- Create: `core/skill_loader.py`
- Create: `skills/learned/__init__.py`
- Create: `skills/learned/_metadata.json`
- Create: `tests/test_skill_loader.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_skill_loader.py
"""Tests for core.skill_loader — dynamic skill loading from skills/learned/."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.skill_loader import SkillLoader


@pytest.fixture()
def loader(tmp_path):
    learned_dir = tmp_path / "skills" / "learned"
    learned_dir.mkdir(parents=True)
    (learned_dir / "__init__.py").write_text("")
    (learned_dir / "_metadata.json").write_text("{}")
    return SkillLoader(str(learned_dir))


class TestSkillLoader:
    def test_empty_dir(self, loader):
        skills = loader.scan()
        assert skills == []

    def test_load_valid_skill(self, loader, tmp_path):
        # 写一个最简 skill 文件
        skill_code = '''
from skills import Skill

class HelloSkill(Skill):
    @property
    def skill_name(self):
        return "hello"

    def get_tool_definitions(self):
        return [{"name": "say_hello", "description": "Say hello", "input_schema": {"type": "object", "properties": {}}}]

    def execute(self, tool_name, tool_input, **ctx):
        return "Hello!"
'''
        skill_path = Path(loader._dir) / "hello_skill.py"
        skill_path.write_text(skill_code)

        skills = loader.scan()
        assert len(skills) == 1
        assert skills[0].skill_name == "hello"

    def test_skip_invalid_file(self, loader):
        # 写一个有语法错误的文件
        bad_path = Path(loader._dir) / "bad_skill.py"
        bad_path.write_text("def broken(")

        skills = loader.scan()
        assert skills == []  # 跳过，不崩溃

    def test_metadata_read_write(self, loader):
        loader.update_metadata("hello", {"taught_by": "allen", "created": "2026-04-03"})
        meta = loader.get_metadata("hello")
        assert meta["taught_by"] == "allen"

    def test_disable_skill(self, loader):
        loader.update_metadata("hello", {"enabled": True})
        loader.update_metadata("hello", {"enabled": False})
        meta = loader.get_metadata("hello")
        assert meta["enabled"] is False
```

- [ ] **Step 2: 实现 SkillLoader**

```python
# core/skill_loader.py
"""Dynamic skill loader — scan, load, and manage learned skills.

Scans skills/learned/ for Python files containing Skill subclasses,
loads them via importlib, and manages per-skill metadata.
"""

from __future__ import annotations

import importlib
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
            List of instantiated Skill objects. Invalid files are skipped.
        """
        skills: list[Skill] = []
        metadata = self._load_metadata()

        for py_file in sorted(self._dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            # Skip disabled skills
            skill_id = py_file.stem
            meta = metadata.get(skill_id, {})
            if not meta.get("enabled", True):
                LOGGER.info("Skipping disabled skill: %s", skill_id)
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
        """Load a single .py file and find the first Skill subclass."""
        spec = importlib.util.spec_from_file_location(f"skills.learned.{path.stem}", path)
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
        return self._load_metadata().get(skill_id, {})

    def update_metadata(self, skill_id: str, updates: dict[str, Any]) -> None:
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
        test_path = Path("tests") / f"test_learned_{skill_id}.py"
        if test_path.exists():
            test_path.unlink()
        data = self._load_metadata()
        data.pop(skill_id, None)
        self._save_metadata(data)
        LOGGER.info("Removed learned skill: %s", skill_id)
        return True
```

- [ ] **Step 3: 跑测试**

Run: `python -m pytest tests/test_skill_loader.py -v`

- [ ] **Step 4: 在 jarvis.py 的 `_register_skills` 中接入**

```python
        # --- Load learned skills ---
        from core.skill_loader import SkillLoader
        self.skill_loader = SkillLoader("skills/learned")
        for skill in self.skill_loader.scan():
            try:
                self.skill_registry.register(skill)
            except Exception as exc:
                self.logger.warning("Failed to register learned skill %s: %s", skill.skill_name, exc)
```

- [ ] **Step 5: Commit**

```bash
git add core/skill_loader.py skills/learned/__init__.py skills/learned/_metadata.json tests/test_skill_loader.py jarvis.py
git commit -m "Add SkillLoader: scan, load, and manage learned skills"
```

---

## Task 6: Claude Code 技能工厂

创造型学习的核心——调用 Claude Code CLI 生成新 skill。

**Files:**
- Create: `core/skill_factory.py`
- Create: `tests/test_skill_factory.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_skill_factory.py
"""Tests for core.skill_factory — Claude Code skill generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.skill_factory import SkillFactory


@pytest.fixture()
def factory(tmp_path):
    return SkillFactory(
        learned_dir=str(tmp_path / "skills" / "learned"),
        project_root=str(tmp_path),
    )


class TestSkillFactory:
    def test_build_prompt(self, factory):
        prompt = factory._build_prompt(
            description="查航班信息",
            skill_abc_source="class Skill(ABC): ...",
            example_skill_source="class WeatherSkill(Skill): ...",
        )
        assert "查航班" in prompt
        assert "Skill" in prompt

    def test_validate_file_clean(self, factory, tmp_path):
        # 写一个干净的 skill 文件
        skill_path = tmp_path / "skills" / "learned" / "test_skill.py"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text('from skills import Skill\nclass TestSkill(Skill):\n    pass\n')
        errors = factory._security_scan(str(skill_path))
        assert errors == []

    def test_validate_file_dangerous(self, factory, tmp_path):
        skill_path = tmp_path / "skills" / "learned" / "evil_skill.py"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text('import os\nos.system("rm -rf /")\n')
        errors = factory._security_scan(str(skill_path))
        assert len(errors) > 0
        assert any("os.system" in e for e in errors)

    def test_validate_blocks_subprocess(self, factory, tmp_path):
        skill_path = tmp_path / "skills" / "learned" / "sub_skill.py"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text('import subprocess\nsubprocess.run(["ls"])\n')
        errors = factory._security_scan(str(skill_path))
        assert len(errors) > 0

    def test_validate_blocks_eval(self, factory, tmp_path):
        skill_path = tmp_path / "skills" / "learned" / "eval_skill.py"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text('eval("1+1")\n')
        errors = factory._security_scan(str(skill_path))
        assert len(errors) > 0
```

- [ ] **Step 2: 实现 SkillFactory**

```python
# core/skill_factory.py
"""Claude Code 技能工厂 — 调用 CC CLI 生成新 skill 文件。

流程：准备上下文 → 调 claude CLI → 安全扫描 → pytest → 热加载
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# 安全黑名单：这些模式在 learned skill 中禁止使用
_DANGEROUS_PATTERNS = [
    (r'\bos\.system\b', "os.system (use requests for HTTP calls)"),
    (r'\bos\.popen\b', "os.popen"),
    (r'\bsubprocess\b', "subprocess module"),
    (r'\beval\s*\(', "eval()"),
    (r'\bexec\s*\(', "exec()"),
    (r'\b__import__\b', "__import__()"),
    (r'\bopen\s*\(.*(["\']/|["\']\.\.)', "file write outside data/"),
    (r'\bshutil\b', "shutil module"),
    (r'\bpickle\b', "pickle module (security risk)"),
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
        on_status: Any = None,
    ) -> dict[str, Any]:
        """Generate a new skill from a description.

        Args:
            description: What the skill should do (user's words).
            skill_name_hint: Optional suggested skill name.
            on_status: Callback(status_str) for progress updates.

        Returns:
            {"success": bool, "skill_name": str, "message": str, "path": str}
        """
        def status(msg: str) -> None:
            LOGGER.info("SkillFactory: %s", msg)
            if on_status:
                on_status(msg)

        # 1. Read Skill ABC and example
        status("准备上下文...")
        abc_source = self._read_file("skills/__init__.py")
        example_source = self._read_file("skills/weather.py")

        # 2. Build prompt
        prompt = self._build_prompt(description, abc_source, example_source)

        # 3. Call Claude Code
        status("正在学习...")
        skill_id = skill_name_hint or self._slugify(description)
        skill_path = self._dir / f"{skill_id}.py"
        test_path = self._root / "tests" / f"test_learned_{skill_id}.py"

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
                return {"success": False, "skill_name": skill_id,
                        "message": f"Claude Code 执行失败: {result.stderr[:200]}"}
        except FileNotFoundError:
            return {"success": False, "skill_name": skill_id,
                    "message": "Claude Code CLI 未安装"}
        except subprocess.TimeoutExpired:
            return {"success": False, "skill_name": skill_id,
                    "message": "Claude Code 超时（120s）"}

        # 4. Verify file was created
        if not skill_path.exists():
            return {"success": False, "skill_name": skill_id,
                    "message": f"未生成 skill 文件: {skill_path}"}

        # 5. Security scan
        status("安全检查...")
        security_errors = self._security_scan(str(skill_path))
        if security_errors:
            skill_path.unlink(missing_ok=True)
            return {"success": False, "skill_name": skill_id,
                    "message": f"安全检查未通过: {'; '.join(security_errors)}"}

        # 6. Run tests (if generated)
        if test_path.exists():
            status("运行测试...")
            test_result = subprocess.run(
                ["python", "-m", "pytest", str(test_path), "-v", "--tb=short"],
                capture_output=True, text=True, timeout=30,
                cwd=str(self._root),
            )
            if test_result.returncode != 0:
                return {"success": False, "skill_name": skill_id,
                        "message": f"测试未通过:\n{test_result.stdout[-500:]}",
                        "path": str(skill_path)}

        status("学会了！")
        return {"success": True, "skill_name": skill_id,
                "message": "技能学习成功", "path": str(skill_path)}

    def _build_prompt(
        self, description: str, skill_abc_source: str, example_skill_source: str,
    ) -> str:
        return f"""你需要为 Jarvis 语音助手写一个新的 skill。

## 需求
{description}

## Skill 接口（必须继承）
```python
{skill_abc_source}
```

## 范例 skill（参考格式）
```python
{example_skill_source}
```

## 要求
1. 在 skills/learned/ 目录下创建一个 .py 文件，文件名用英文下划线命名
2. 继承 Skill ABC，实现 skill_name、get_tool_definitions、execute
3. 在 tests/ 目录下创建对应的测试文件 test_learned_<name>.py
4. tool_input 参数从 Claude 传入，execute 返回文本结果
5. 网络请求用 requests，设置 timeout=10
6. 不要使用 os.system、subprocess、eval、exec
7. 不要读写 core/ 目录下的文件
8. 用 logging 不用 print
9. 代码中加 type hints

只写文件，不要输出其他内容。"""

    def _security_scan(self, file_path: str) -> list[str]:
        """Scan a file for dangerous patterns."""
        content = Path(file_path).read_text()
        errors = []
        for pattern, desc in _DANGEROUS_PATTERNS:
            if re.search(pattern, content):
                errors.append(desc)
        return errors

    def _read_file(self, rel_path: str) -> str:
        path = self._root / rel_path
        if path.exists():
            return path.read_text()
        return f"# File not found: {rel_path}"

    def _slugify(self, text: str) -> str:
        """Convert description to a valid Python identifier."""
        import unicodedata
        # Simple: take first few chars, remove non-ascii, lowercase
        slug = text[:30].strip().lower()
        slug = re.sub(r'[^\w\s]', '', slug)
        slug = re.sub(r'\s+', '_', slug)
        slug = re.sub(r'[^\x00-\x7f]', '', slug)  # remove non-ascii
        return slug or "custom_skill"
```

- [ ] **Step 3: 跑测试**

Run: `python -m pytest tests/test_skill_factory.py -v`

- [ ] **Step 4: Commit**

```bash
git add core/skill_factory.py tests/test_skill_factory.py
git commit -m "Add SkillFactory: Claude Code CLI skill generation with security scan"
```

---

## Task 7: 在 jarvis.py 中接入完整学习流程

把学习路由、配置型、组合型、创造型全部接入 jarvis pipeline。

**Files:**
- Modify: `jarvis.py`

- [ ] **Step 1: 在 `__init__` 中初始化学习组件**

```python
        # --- Learning router + skill factory ---
        from core.learning_router import LearningRouter
        from core.skill_factory import SkillFactory
        self.learning_router = LearningRouter(
            skill_names=list(self.skill_registry.skill_names),
        )
        self.skill_factory = SkillFactory(
            learned_dir="skills/learned",
            project_root=str(self.config_path.parent),
        )
```

- [ ] **Step 2: 在 `_handle_utterance_inner` 中加学习处理**

在 Level 1 之后、keyword 检查之前：

```python
        # 4c. Learning intent detection
        if hasattr(self, 'learning_router'):
            learning = self.learning_router.detect(text)
            if learning:
                self.logger.info("Learning intent: mode=%s", learning.mode)
                learn_response = self._handle_learning(text, learning, user_id, detected_emotion)
                if learn_response:
                    self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                    print(f"🤖 Jarvis: {learn_response}")
                    self._speak_nonblocking(learn_response, emotion=detected_emotion)
                    # 存入对话历史
                    history.append({"role": "user", "content": text})
                    history.append({"role": "assistant", "content": learn_response})
                    self.conversation_store.replace(session_id, history)
                    return learn_response
```

- [ ] **Step 3: 实现 `_handle_learning` 方法**

```python
    def _handle_learning(self, text: str, intent: LearningIntent,
                         user_id: str | None, emotion: str) -> str | None:
        """Handle a learning intent — config/compose/create."""
        if intent.mode == "config":
            return self._learn_config(text, intent)
        elif intent.mode == "compose":
            return self._learn_compose(text, intent)
        elif intent.mode == "create":
            return self._learn_create(text, intent, user_id)
        return None

    def _learn_config(self, text: str, intent: LearningIntent) -> str | None:
        """配置型：用 LLM 解析 trigger/skill/params，创建 skill_alias 规则。"""
        # 简单解析：直接用意图中提取的 trigger
        if not intent.trigger or not self.rule_manager:
            return None
        # 让云端 LLM 解析完整的 action
        # 目前简化为：trigger → 走云端处理
        return None  # 交给云端 LLM 处理

    def _learn_compose(self, text: str, intent: LearningIntent) -> str | None:
        """组合型：交给云端 LLM 处理，它会调用 automation tool。"""
        return None  # 交给云端 LLM 处理（现有 automation skill 支持）

    def _learn_create(self, text: str, intent: LearningIntent,
                      user_id: str | None) -> str:
        """创造型：调用 Claude Code 技能工厂。"""
        self.speak("好的，我去学一下，稍等。")

        result = self.skill_factory.create(
            description=intent.description,
            on_status=lambda msg: self.logger.info("SkillFactory: %s", msg),
        )

        if result["success"]:
            # 热加载注册
            try:
                from core.skill_loader import SkillLoader
                loader = SkillLoader("skills/learned")
                new_skills = loader.scan()
                for skill in new_skills:
                    if skill.skill_name not in self.skill_registry.skill_names:
                        self.skill_registry.register(skill)
                        self.learning_router.update_skills(list(self.skill_registry.skill_names))
                # 记录到记忆
                if user_id:
                    self.behavior_log.log(user_id, "skill_learned", {
                        "skill": result["skill_name"],
                        "description": intent.description,
                    })
            except Exception as exc:
                self.logger.warning("Failed to load new skill: %s", exc)
                return f"技能文件生成了但加载失败：{exc}"

            return f"学会了！现在我可以{intent.description}了，要试试吗？"
        else:
            return f"没学会，{result['message']}"
```

- [ ] **Step 4: 跑全量测试**

Run: `python -m pytest tests/ -q`

- [ ] **Step 5: Commit**

```bash
git add jarvis.py
git commit -m "Wire learning router, config/compose/create handlers into jarvis pipeline"
```

---

## Task 8: 技能管理 — 查询、禁用、删除

用户通过对话管理已学技能。

**Files:**
- Modify: `skills/memory_skill.py` — 或创建新的 `skills/skill_mgmt.py`

- [ ] **Step 1: 创建 SkillManagementSkill**

```python
# skills/skill_mgmt.py
"""Skill management — list, disable, remove learned skills."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class SkillManagementSkill(Skill):
    """Allows users to query and manage learned skills."""

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
                "description": "List all skills (built-in and learned). Use when user asks what you can do.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "disable_skill",
                "description": "Disable or remove a learned skill. Use when user says to forget a skill.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "Name of the skill to disable."},
                        "delete": {"type": "boolean", "description": "True to permanently delete, False to just disable."},
                    },
                    "required": ["skill_name"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **ctx: Any) -> str:
        if tool_name == "list_skills":
            names = self._registry.skill_names
            builtin = [n for n in names if not self._is_learned(n)]
            learned = [n for n in names if self._is_learned(n)]
            parts = [f"内置技能（{len(builtin)}个）：{', '.join(builtin)}"]
            if learned:
                parts.append(f"学会的技能（{len(learned)}个）：{', '.join(learned)}")
            else:
                parts.append("还没学会新技能。")
            return "\n".join(parts)

        if tool_name == "disable_skill":
            name = tool_input.get("skill_name", "")
            delete = tool_input.get("delete", False)
            if not self._is_learned(name):
                return f"'{name}' 是内置技能，不能删除。"
            if delete:
                self._loader.remove_skill(name)
                return f"已永久删除技能 '{name}'。"
            else:
                self._loader.update_metadata(name, {"enabled": False})
                return f"已禁用技能 '{name}'，重启后生效。"

        return f"Unknown tool: {tool_name}"

    def _is_learned(self, name: str) -> bool:
        meta = self._loader.get_metadata(name)
        return bool(meta)  # learned skills have metadata
```

- [ ] **Step 2: 在 jarvis.py 中注册**

```python
        from skills.skill_mgmt import SkillManagementSkill
        self.skill_registry.register(SkillManagementSkill(self.skill_loader, self.skill_registry))
```

- [ ] **Step 3: 跑测试**
- [ ] **Step 4: Commit**

```bash
git add skills/skill_mgmt.py jarvis.py
git commit -m "Add SkillManagementSkill: list, disable, remove learned skills"
```

---

## Task 9: 端到端测试 + 最终验证

- [ ] **Step 1: 创建 `tests/test_learning_e2e.py`**

测试完整流程：学习意图检测 → 分流 → skill_alias 创建 → skill loader → 技能管理。

- [ ] **Step 2: 跑全量测试**

Run: `python -m pytest tests/ -v`

- [ ] **Step 3: Ruff check**

Run: `ruff check core/learning_router.py core/skill_factory.py core/skill_loader.py skills/skill_mgmt.py`

- [ ] **Step 4: 最终 commit**

```bash
git add -A
git commit -m "T1 complete: skill learning — config, compose, create via Claude Code"
```
