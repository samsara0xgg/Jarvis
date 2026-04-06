"""学习意图检测 — 判断用户是否在教小月新技能。

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

_CREATE_KEYWORDS = ["学会", "学一下", "去学", "帮我加一个", "新增一个", "添加一个"]

_CONFIG_PATTERNS = [
    re.compile(r"(?:以后|以后我说|以后说)(?:我说)?[「「]?(.+?)[」」]?(?:就|就帮我|你就|就给我|帮我)"),
    re.compile(r"记住每次(?:我说|说)?[「「]?(.+?)[」」]?(?:就|就帮我)"),
]

_SCHEDULE_KEYWORDS = ["每天", "每周", "每个月", "每隔", "定时"]


@dataclass
class LearningIntent:
    """学习意图检测结果。"""
    mode: str  # "config" | "compose" | "create"
    trigger: str = ""
    description: str = ""
    raw_text: str = ""


class LearningRouter:
    """检测和分类学习意图。"""

    def __init__(self, skill_names: list[str] | None = None) -> None:
        self._skill_names = set(skill_names or [])

    def update_skills(self, skill_names: list[str]) -> None:
        self._skill_names = set(skill_names)

    def detect(self, text: str) -> LearningIntent | None:
        text = text.strip()

        # 1. Create: explicit "learn" keywords (highest priority)
        for kw in _CREATE_KEYWORDS:
            if kw in text:
                desc = text.split(kw, 1)[-1].strip()
                return LearningIntent(mode="create", description=desc, raw_text=text)

        # 2. Config: "以后说X就Y" pattern
        for pattern in _CONFIG_PATTERNS:
            match = pattern.search(text)
            if match:
                trigger = match.group(1).strip()
                return LearningIntent(mode="config", trigger=trigger, description=text, raw_text=text)

        # 3. Compose: schedule keywords
        if any(kw in text for kw in _SCHEDULE_KEYWORDS):
            return LearningIntent(mode="compose", description=text, raw_text=text)

        return None
