"""ADR 0037 acceptance: activities are sorted once per catalog and the view is derived.

Data-driven: a TimeSink-shaped store, a real git repository, the real event
log and HTTP app; only the model is a scripted sorter that answers by label,
so the checks assert on the persisted answers and the served view.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.decision.llm import ChatResult, ToolCall
from jarvis.decision.projects import ASSIGN_TOOL_NAME
from jarvis.runtime.projects import ProjectsService
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.projects import Project, parse_catalog
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.integration.test_timesink_activity import source

if TYPE_CHECKING:
    import sqlite3

__all__ = ["source"]

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 9, 23, 18, 0, tzinfo=ZONE)
_SHORT = re.compile(r"^\[(a\d+)\] (.*)$", re.MULTILINE)


def _stamp(local: str) -> str:
    """A local wall-clock instant in TimeSink's stored form (UTC, milliseconds, no suffix)."""
    at = datetime.fromisoformat(local).replace(tzinfo=ZONE).astimezone(UTC)
    return at.isoformat(sep=" ", timespec="milliseconds").removesuffix("+00:00")


def _span(  # noqa: PLR0913 — the columns TimeSink writes.
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
        for short, line in _SHORT.findall(material):
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


class Rig:
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
def rig(tmp_path: Path, source: sqlite3.Connection) -> Rig:
    """A week with a chat, a terminal session, a new tab, and spans at both window edges."""
    chat = {"bundle": "com.openai.chat", "name": "ChatGPT", "title": "ChatGPT"}
    chat["document"] = "COOP！"
    term = {"bundle": "com.mitchellh.ghostty", "name": "Ghostty", "title": "cc | jarvis full audit"}
    _span(source, "2026-09-22T23:50:00", "2026-09-23T00:10:00", **chat)  # crosses midnight
    _span(source, "2026-09-23T10:00:00", "2026-09-23T10:30:00", **chat)
    _span(source, "2026-09-16T23:30:00", "2026-09-17T00:30:00", **term)  # half before the window
    _span(source, "2026-09-21T14:00:00", "2026-09-21T15:00:00", **term)
    _span(source, "2026-09-10T09:00:00", "2026-09-10T10:00:00", **term)  # outside the window
    _span(
        source, "2026-09-23T11:00:00", "2026-09-23T11:05:00",
        bundle="com.google.Chrome", name="Google Chrome", title="New Tab", domain="newtab",
    )
    return Rig(tmp_path, tmp_path / "timesink.sqlite")


def _project(view: dict[str, Any], pid: str) -> dict[str, Any]:
    return next(p for p in view["projects"] if p["id"] == pid)


def test_sorting_saves_answers_and_the_view_is_derived(rig: Rig) -> None:
    before = rig.service.read()
    week = [1800, 0, 0, 0, 3600, 600, 2700]
    assert before["unsorted"] == {"seconds": 3000 + 5400 + 300, "days": week, "count": 3}
    assert all(p["seconds"] == 0 for p in before["projects"])
    assert rig.sorter.calls == 0  # reading never calls the model

    view = rig.service.refresh()
    assert view["outcome"] == "classified"
    assert rig.sorter.calls == 1
    [event] = rig.events()
    assert event["trigger"] == "dashboard"
    assert event["model"] == "scripted"
    assert sorted((a["label"], a["project"]) for a in event["assignments"]) == [
        ("COOP！", "job-search"),
        ("New Tab", None),
        ("cc | jarvis full audit", "jarvis"),
    ]
    assert view["days"] == [f"2026-09-{d}" for d in range(17, 24)]
    job, jarvis, school = (_project(view, p) for p in ("job-search", "jarvis", "school"))
    assert (job["seconds"], job["today_seconds"]) == (3000, 2400)
    assert job["days"] == [0, 0, 0, 0, 0, 600, 2400]  # the midnight span is split
    assert (jarvis["seconds"], jarvis["days"]) == (5400, [1800, 0, 0, 0, 3600, 0, 0])
    assert [p["id"] for p in view["projects"]] == ["jarvis", "job-search", "school"]
    assert school == {**school, "seconds": 0, "last_seen": None, "recent": []}
    assert job["recent"] == [
        {
            "app": "ChatGPT",
            "label": "COOP！",
            "seconds": 3000,
            "last_seen": "2026-09-23T10:30:00-07:00",
        }
    ]
    assert jarvis["commits"]["count"] == 2
    assert [(c["subject"], c["on_main"], c["repo"]) for c in jarvis["commits"]["items"]] == [
        ("wip: feature", False, "jarvis-repo"),
        ("feat: audit", True, "jarvis-repo"),
    ]
    assert view["other"] == {"seconds": 300, "days": [0, 0, 0, 0, 0, 0, 300], "count": 1}
    assert view["unsorted"]["seconds"] == 0
    assert view["coverage"] == {"timesink": "available", "git": "available"}
    assert view["latest_observed_at"] == "2026-09-23T11:05:00-07:00"
    assert before["sorted_at"] is None
    assert view["sorted_at"] is not None
    assert rig.service.read()["projects"] == view["projects"]


def test_only_new_activities_reach_the_model(rig: Rig, source: sqlite3.Connection) -> None:
    rig.service.refresh()
    again = rig.service.refresh()
    assert again["outcome"] == "reused"
    assert rig.sorter.calls == 1
    assert len(rig.events()) == 1

    _span(
        source, "2026-09-23T12:00:00", "2026-09-23T12:20:00",
        bundle="com.openai.chat", name="ChatGPT", title="ChatGPT", document="准备这份工作的简历",
    )
    _span(source, "2026-09-23T13:00:00", "2026-09-23T13:10:00",
          bundle="com.openai.chat", name="ChatGPT", title="ChatGPT", document="COOP！")
    view = rig.service.refresh()
    assert view["outcome"] == "classified"
    assert rig.sorter.calls == 2
    assert "准备这份工作的简历" in rig.sorter.materials[-1]
    assert "COOP" not in rig.sorter.materials[-1]  # already answered, extended time only
    assert _project(view, "job-search")["seconds"] == 3000 + 600
    assert view["other"]["seconds"] == 300 + 1200


def test_editing_the_catalog_sorts_everything_again(rig: Rig) -> None:
    rig.service.refresh()
    assert rig.build(reverse=True).read()["unsorted"]["count"] == 0  # reordering is not an edit
    edited = rig.build(hints="co-op, internships, resumes")
    assert edited.read()["unsorted"]["count"] == 3
    assert edited.refresh()["outcome"] == "classified"
    assert rig.sorter.calls == 2
    # The old catalog's answers still stand for a service built on it.
    assert _project(rig.service.read(), "job-search")["seconds"] == 3000


@pytest.mark.parametrize(
    ("reply", "sorted_count"),
    [
        ({"groups": [{"project": "atlantis", "keys": ["a1", "a2", "a3"]}]}, 0),
        ({"groups": [{"project": "none", "keys": ["a1", "zz9", 7]}]}, 1),
        (  # an unhashable id drops only its own group
            {"groups": [
                {"project": ["jarvis"], "keys": ["a1"]},
                {"project": "none", "keys": ["a2"]},
            ]},
            1,
        ),
    ],
)
def test_unknown_ids_and_keys_are_dropped(
    rig: Rig, reply: dict[str, Any], sorted_count: int
) -> None:
    rig.sorter.reply = reply
    view = rig.service.refresh()
    assert view["outcome"] == "classified"
    assert view["unsorted"]["count"] == 3 - sorted_count
    assert sum(len(e["assignments"]) for e in rig.events()) == sorted_count


@pytest.mark.parametrize(
    "bad", [{"groups": "jarvis"}, {"assignments": []}, {"groups": [{"project": "none"}]}]
)
def test_a_malformed_reply_saves_nothing(rig: Rig, bad: dict[str, Any]) -> None:
    rig.sorter.reply = bad
    view = rig.service.refresh()
    assert view["outcome"] == "failed"
    assert "0 batch(es) saved" in view["error"]
    assert rig.events() == []
    assert view["unsorted"]["count"] == 3


def test_a_later_batch_failing_keeps_the_earlier_ones(
    rig: Rig, source: sqlite3.Connection
) -> None:
    for n in range(305):  # 308 activities: one batch of 300, then 8
        minute = f"{n // 60 + 1:02d}:{n % 60:02d}"
        _span(
            source, f"2026-09-20T{minute}:00", f"2026-09-20T{minute}:59",
            bundle="com.google.Chrome", name="Google Chrome", title=f"page {n}",
        )
    rig.sorter.malformed_from = 2
    failed = rig.service.refresh()
    assert failed["outcome"] == "failed"
    assert failed["error"].startswith("1 batch(es) saved")
    [event] = rig.events()
    assert len(event["assignments"]) == 300
    assert failed["unsorted"]["count"] == 8
    rig.sorter.malformed_from = None
    done = rig.service.refresh()
    assert done["outcome"] == "classified"
    assert len(_SHORT.findall(rig.sorter.materials[-1])) == 8  # only the leftovers
    assert done["unsorted"]["count"] == 0


def test_provider_failure_and_missing_store(rig: Rig, tmp_path: Path) -> None:
    rig.sorter.fail = True
    failed = rig.service.refresh()
    assert failed["outcome"] == "failed"
    assert "ConnectionError" in failed["error"]
    assert rig.service.read()["outcome"] == "failed"  # the last run is reported until the next
    blind = ProjectsService(
        event_log_path=rig.log, timesink_path=tmp_path / "absent.sqlite",
        projects=(Project("jarvis", "Jarvis", (str(rig.repo),), ""),),
        sorter=rig.sorter, model="scripted", tz=ZONE, clock=lambda: NOW,
    )
    view = blind.refresh()
    assert view["outcome"] == "no_evidence"
    assert view["coverage"]["timesink"] == "unavailable"
    assert view["projects"][0]["commits"]["count"] == 2  # commits do not depend on TimeSink


def test_a_refresh_during_a_run_joins_it(rig: Rig) -> None:
    rig.sorter.release.clear()
    results: list[dict[str, Any]] = []
    first = threading.Thread(target=lambda: results.append(rig.service.refresh()))
    first.start()
    assert rig.sorter.started.wait(timeout=5)
    assert rig.service.read()["refreshing"] is True
    second = threading.Thread(target=lambda: results.append(rig.service.refresh()))
    second.start()
    rig.sorter.release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert rig.sorter.calls == 1
    assert sorted(r.get("joined", False) for r in results) == [False, True]
    assert {r["outcome"] for r in results} == {"classified"}


def test_http_routes_serve_the_view(rig: Rig) -> None:
    async def read() -> dict[str, Any]:
        return rig.service.read()

    async def refresh() -> dict[str, Any]:
        return rig.service.refresh()

    deps = InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        projects_read=read,
        projects_refresh=refresh,
    )
    with TestClient(create_app(deps)) as client:
        assert client.get("/inherent/projects").json()["unsorted"]["count"] == 3
        assert rig.sorter.calls == 0
        refreshed = client.post("/inherent/projects/refresh").json()
        assert refreshed["outcome"] == "classified"
        assert _project(refreshed, "jarvis")["seconds"] == 5400
    bare = InherentDeps(submit_callable=lambda _text: None, broadcaster=InherentBroadcaster())
    with TestClient(create_app(bare)) as client:
        assert client.get("/inherent/projects").status_code == 404


def test_shipped_catalog_boots() -> None:
    raw = yaml.safe_load(Path("config/jarvis.yaml").read_text(encoding="utf-8"))["projects"]
    catalog = parse_catalog(raw)
    assert [p.id for p in catalog][:3] == ["job-search", "school", "jarvis"]
    assert all(not r.startswith("~") for p in catalog for r in p.repos)
    cases = (
        [{"id": "Jarvis", "name": "x"}],  # not a lowercase slug
        [{"id": "a", "name": "x"}] * 2,  # repeated id
        [{"id": "none", "name": "x"}],  # reserved for "no project"
    )
    for broken in cases:
        with pytest.raises(ValueError, match="projects entry"):
            parse_catalog(broken)
