"""The projects service over a TimeSink fixture and a git repository, and a scripted sorter."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.llm import ChatResult, ToolCall
from jarvis.decision.projects import ASSIGN_TOOL_NAME
from jarvis.runtime.projects import ProjectsService
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.projects import Project

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")


NOW = datetime(2026, 9, 23, 18, 0, tzinfo=ZONE)


SHORT = re.compile(r"^\[(a\d+)\] (.*)$", re.MULTILINE)


def _stamp(local: str) -> str:
    """A local wall-clock instant in TimeSink's stored form (UTC, milliseconds, no suffix)."""
    at = datetime.fromisoformat(local).replace(tzinfo=ZONE).astimezone(UTC)
    return at.isoformat(sep=" ", timespec="milliseconds").removesuffix("+00:00")


def span(  # noqa: PLR0913 — the columns TimeSink writes.
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    bundle: str,
    name: str,
    title: str | None = None,
    document: str | None = None,
    domain: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO span(start,end,appBundleID,appName,title,url,domain,document) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (_stamp(start), _stamp(end), bundle, name, title, None, domain, document),
    )
    conn.commit()


def _commit(repo: Path, local: str, subject: str) -> None:
    stamp = datetime.fromisoformat(local).replace(tzinfo=ZONE).isoformat()
    env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp, "PATH": "/usr/bin:/bin"}
    subprocess.run(  # noqa: S603 — fixed argv on a temp repository.
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit",  # noqa: S607
         "--allow-empty", "-q", "-m", subject],
        check=True,
        env=env,
    )


class ScriptedSorter:
    """Answers by label like a model would; counts calls and keeps each request's material."""

    RULES = (("COOP", "job-search"), ("jarvis full audit", "jarvis"))

    def __init__(self) -> None:
        self.calls = 0
        self.materials: list[str] = []
        self.reply: dict[str, Any] | None = None
        self.malformed_from: int | None = None
        self.fail = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def analyze(
        self,
        conn: sqlite3.Connection,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Any = None,  # noqa: ANN401 — the forced tool catalog.
    ) -> ChatResult:
        """Mimic one forced assign_projects call."""
        del conn, system
        assert [t["name"] for t in tools] == [ASSIGN_TOOL_NAME]
        self.calls += 1
        material = str(messages[0]["content"])
        self.materials.append(material)
        self.started.set()
        self.release.wait(timeout=5)
        if self.fail:
            msg = "provider down"
            raise ConnectionError(msg)
        groups: dict[str, list[str]] = {}
        for short, line in SHORT.findall(material):
            project = next((pid for word, pid in self.RULES if word in line), "none")
            groups.setdefault(project, []).append(short)
        reply = self.reply or {"groups": [{"project": p, "keys": k} for p, k in groups.items()]}
        if self.malformed_from is not None and self.calls >= self.malformed_from:
            reply = {"groups": "not a list"}
        return ChatResult(
            text=None,
            tool_calls=(ToolCall("c1", ASSIGN_TOOL_NAME, json.dumps(reply)),),
            finish_reason="tool_calls",
            input_tokens=10,
            output_tokens=10,
            raw={},
        )


class ProjectsRig:
    """One service over a temp event log, the TimeSink fixture and a real git repository."""

    def __init__(self, root: Path, timesink: Path) -> None:
        """Seed the repository and wire the service exactly as the composition root does."""
        self.log = root / "events.sqlite"
        open_event_log(self.log).close()
        self.repo = root / "jarvis-repo"
        git = ["git", "-C", str(self.repo)]
        subprocess.run([*git[:1], "init", "-q", "-b", "main", str(self.repo)], check=True)  # noqa: S603
        _commit(self.repo, "2026-09-10T12:00:00", "old work")
        _commit(self.repo, "2026-09-21T16:00:00", "feat: audit")
        subprocess.run([*git, "switch", "-q", "-c", "feature"], check=True)  # noqa: S603
        _commit(self.repo, "2026-09-23T09:00:00", "wip: feature")
        self.timesink = timesink
        self.sorter = ScriptedSorter()
        self.service = self.build()

    def build(self, *, hints: str = "co-op", reverse: bool = False) -> ProjectsService:
        """A service over this rig's stores; ``hints`` changes the catalog fingerprint."""
        catalog = (
            Project("job-search", "求职", (), hints),
            Project("jarvis", "Jarvis", (str(self.repo),), "Jarvis runtime"),
            Project("school", "学业", (), "UVic"),
        )
        return ProjectsService(
            event_log_path=self.log,
            timesink_path=self.timesink,
            projects=catalog[::-1] if reverse else catalog,
            sorter=self.sorter,
            model="scripted",
            tz=ZONE,
            clock=lambda: NOW,
        )

    def events(self) -> list[dict[str, Any]]:
        """Every persisted sorting answer, oldest first."""
        conn = open_event_log(self.log)
        try:
            kinds = ("project.activity_classified",)
            return [dict(e.payload) for e in iter_events_of_types(conn, kinds)]
        finally:
            conn.close()


@pytest.fixture
def rig(tmp_path: Path, source: sqlite3.Connection) -> ProjectsRig:
    """A week with a chat, a terminal session, a new tab, and spans at both window edges."""
    chat = {"bundle": "com.openai.chat", "name": "ChatGPT", "title": "ChatGPT"}
    chat["document"] = "COOP！"
    term = {"bundle": "com.mitchellh.ghostty", "name": "Ghostty", "title": "cc | jarvis full audit"}
    span(source, "2026-09-22T23:50:00", "2026-09-23T00:10:00", **chat)  # crosses midnight
    span(source, "2026-09-23T10:00:00", "2026-09-23T10:30:00", **chat)
    span(source, "2026-09-16T23:30:00", "2026-09-17T00:30:00", **term)  # half before the window
    span(source, "2026-09-21T14:00:00", "2026-09-21T15:00:00", **term)
    span(source, "2026-09-10T09:00:00", "2026-09-10T10:00:00", **term)  # outside the window
    span(
        source, "2026-09-23T11:00:00", "2026-09-23T11:05:00",
        bundle="com.google.Chrome", name="Google Chrome", title="New Tab", domain="newtab",
    )
    return ProjectsRig(tmp_path, tmp_path / "timesink.sqlite")


def project_of(view: dict[str, Any], pid: str) -> dict[str, Any]:
    return next(p for p in view["projects"] if p["id"] == pid)
