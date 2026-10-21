"""The daily-report world: a canned reporter, whole items and a real git repository."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time as clock
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.decision.daily_report import (
    DETAILS_TOOL_NAME,
    JUDGE_TOOL_NAME,
    REPORT_TOOL_NAME,
    SUMMARY_TOOL_NAME,
)
from jarvis.decision.llm import ChatResult, ToolCall

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

ZONE = "America/Vancouver"


TZ = ZoneInfo(ZONE)


DAY = date(2026, 9, 19)


# 2026-09-19 in Vancouver is UTC-7, so the local day is [07:00Z, next 07:00Z).
NOW = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


JUDGE = [JUDGE_TOOL_NAME]


SUMMARY = [SUMMARY_TOOL_NAME]


REPORT: dict[str, Any] = {
    "items": [
        {
            "title": "每日工具的读取修复",
            "activity": "改完 TimeSink 读取边界并提交。",
            "progress": [{"part": None, "status": "completed", "refs": ["g1"]}],
        },
        {
            "title": "浏览招聘页面",
            "activity": "在 Chrome 里翻了职位列表。",
            "progress": [{"part": None, "status": "browsed", "refs": ["a1", "s1"]}],
        },
        {
            "title": "演示界面显示已部署",
            "activity": "屏幕上出现“部署成功”。",
            "progress": [{"part": None, "status": "completed", "refs": ["s1"]}],
        },
        {
            "title": "没有依据的事项",
            "activity": "引用了不存在的键。",
            "progress": [{"part": None, "status": "discussed", "refs": ["zz9"]}],
        },
    ],
    "decisions": [
        {"text": "待办先放在本地。", "rationale": "Allen 自己说的。", "refs": ["r1"]},
    ],
    "open_items": [{"text": "屏幕采集还没跑满一天。", "refs": []}],
    "user_next_steps": [
        {"text": "明天继续写日报工具。", "refs": ["r1"]},
        {"text": "顺手把 CI 修好。", "refs": ["s1"]},
    ],
    "suggestions": ["可以给屏幕采集加一条健康事件。"],
    "uncertainties": ["有一段时间没有任何记录。"],
}


def whole_item(
    title: str, status: str, refs: list[str], activity: str = "活动与进展。"
) -> dict[str, Any]:
    """An item with one unnamed part: the shape of anything that is not code-plus-deployment."""
    return {
        "title": title,
        "activity": activity,
        "progress": [{"part": None, "status": status, "refs": refs}],
    }


def report_of_items(*items: dict[str, Any]) -> dict[str, Any]:
    """A report of just these items, so a check reads few lines."""
    return {
        "items": list(items),
        "decisions": [],
        "open_items": [],
        "user_next_steps": [],
        "suggestions": [],
        "uncertainties": [],
    }


def one_item(title: str, status: str, refs: list[str]) -> dict[str, Any]:
    """A report of exactly one whole item, so a check reads one status line."""
    return report_of_items(whole_item(title, status, refs))


def provider_call(name: str, payload: dict[str, Any], call_id: str = "c1") -> ChatResult:
    """The provider's tool call around one payload."""
    return ChatResult(
        text=None,
        tool_calls=(ToolCall(call_id, name, json.dumps(payload)),),
        finish_reason="tool_calls",
        input_tokens=1,
        output_tokens=1,
        raw={},
    )


def git_repo(path: Path, commits: list[tuple[str, str, str]]) -> dict[str, str]:
    """A real repository with the given (subject, committed_at, branch) commits; subject -> sha."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }

    def git(*args: str, when: str | None = None) -> str:
        dated = {**env, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else env
        done = subprocess.run(  # noqa: S603 — test fixture over a temporary repository.
            ["git", "-C", str(path), *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            env=dated,
        )
        return done.stdout.strip()

    path.mkdir()
    git("init", "-q", "-b", "main")
    shas = {}
    branches = {"main"}
    current = "main"
    for index, (subject, when, branch) in enumerate(commits):
        if branch != current:
            # A branch is created off main once and returned to as is, never reset.
            git("checkout", "-q", *(["-b", branch, "main"] if branch not in branches else [branch]))
            branches.add(branch)
            current = branch
        (path / f"f{index}").write_text(subject, encoding="utf-8")
        git("add", f"f{index}")
        git("commit", "-q", "-m", subject, when=when)
        shas[subject] = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    return shas


Verdict = tuple[str, str, str | None]


SUPPORTED: Verdict = ("supported", "原文显示这个部分已完成", "page")


class CannedReporter:
    """Answer each tool by name and record what was asked; ``fail`` raises like an outage.

    Drafting rounds follow ``script``/``ask_details`` then return ``report``;
    a check returns ``verdicts[title]`` (one verdict for every part, or one
    per part) or ``verdict``; the summary returns ``main_line``.
    """

    def __init__(self, report: dict[str, Any] | None = None) -> None:
        self.calls = 0
        """Drafting calls made; the check and summary calls are counted in ``catalogs``."""
        self.fail = False
        self.fail_checks = False
        self.malformed = False
        self.malformed_once = False
        self.malformed_checks: set[str] = set()
        self.fail_check_titles: set[str] = set()
        self.check_delay = 0.0
        self.check_spans: list[tuple[float, float]] = []
        """(start, end) of each check call, to show whether they overlapped."""
        self.ask_details: list[str] | None = None
        self.script: list[list[tuple[str, dict[str, Any]]]] = []
        """Tool calls to make on each round before reporting: [[(tool, args), ...], ...]."""
        self.report = report if report is not None else REPORT
        self.verdict: Verdict = SUPPORTED
        self.verdicts: dict[str, Verdict | list[Verdict]] = {}
        self.main_line = "主要在 Jarvis 仓库上改每日工具，并看了一轮招聘页面。"
        self.materials: list[str] = []
        self.replies: list[str] = []
        """Per call, the tool replies it was shown (search hits and details)."""
        self.catalogs: list[list[str]] = []
        self.choices: list[str] = []
        self.checks: list[str] = []
        self.summaries: list[str] = []

    @property
    def material(self) -> str:
        """What the first drafting call was given."""
        return self.materials[0] if self.materials else ""

    def analyze(
        self,
        conn: sqlite3.Connection,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        tool_choice: str,
    ) -> ChatResult:
        """Mimic the model behind whichever tool catalog was offered."""
        del conn
        names = [str(t["name"]) for t in tools]
        self.catalogs.append(names)
        self.choices.append(tool_choice)
        content = "\n".join(str(m.get("content") or "") for m in messages)
        self.materials.append(content)
        self.replies.append(
            "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "tool")
        )
        if self.fail:
            msg = "provider down"
            raise ConnectionError(msg)
        if names == JUDGE:
            return self._judge(content)
        if names == SUMMARY:
            self.summaries.append(content)
            return provider_call(SUMMARY_TOOL_NAME, {"main_line": self.main_line})
        self.calls += 1
        self.system = system
        payload: dict[str, Any] = self.report
        name = REPORT_TOOL_NAME
        if self.malformed or (self.malformed_once and self.calls == 1):
            payload = {"summary": "少了字段"}
        elif self.ask_details is not None and self.calls == 1:
            payload, name = {"keys": self.ask_details}, DETAILS_TOOL_NAME
        elif self.calls <= len(self.script):
            calls = tuple(
                ToolCall(f"c{self.calls}-{index}", tool, json.dumps(args))
                for index, (tool, args) in enumerate(self.script[self.calls - 1])
            )
            return ChatResult(
                text=None,
                tool_calls=calls,
                finish_reason="tool_calls",
                input_tokens=10,
                output_tokens=10,
                raw={},
            )
        return provider_call(name, payload, f"c{self.calls}")

    def _judge(self, content: str) -> ChatResult:
        self.checks.append(content)
        started = clock.monotonic()
        clock.sleep(self.check_delay)
        self.check_spans.append((started, clock.monotonic()))
        if self.fail_checks:
            msg = "provider down during the checks"
            raise ConnectionError(msg)
        title = content.partition("\n")[0].removeprefix("Item: ")
        if title in self.fail_check_titles:
            msg = "provider down for one check"
            raise ConnectionError(msg)
        if title in self.malformed_checks:
            return provider_call(JUDGE_TOOL_NAME, {"verdicts": "not a list"})
        claimed = content.split("Parts claimed done:", 1)[1].split("The cited originals:", 1)[0]
        numbers = [int(n) for n in re.findall(r"^(\d+)\. ", claimed, re.MULTILINE)]
        chosen = self.verdicts.get(title, self.verdict)
        rows = []
        for number in numbers:
            verdict, shows, screen = chosen[number - 1] if isinstance(chosen, list) else chosen
            rows.append({"part": number, "verdict": verdict, "shows": shows, "screen": screen})
        return provider_call(JUDGE_TOOL_NAME, {"verdicts": rows})
