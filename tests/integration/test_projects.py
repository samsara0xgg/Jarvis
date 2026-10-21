"""ADR 0037 acceptance: activities are sorted once per catalog and the view is derived.

Data-driven: a TimeSink-shaped store, a real git repository, the real event
log and HTTP app; only the model is a scripted sorter that answers by label,
so the checks assert on the persisted answers and the served view.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.runtime.config import load_runtime_config
from jarvis.runtime.projects import ProjectsService
from jarvis.state.projects import Project, parse_catalog
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from tests.support import projects as projects_support
from tests.support.projects import NOW, SHORT, ZONE, ProjectsRig, project_of, span
from tests.support.timesink import source

if TYPE_CHECKING:
    import sqlite3

rig = projects_support.rig  # the fixture, found by name in this module


__all__ = ["source"]


def test_sorting_saves_answers_and_the_view_is_derived(rig: ProjectsRig) -> None:
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
    job, jarvis, school = (project_of(view, p) for p in ("job-search", "jarvis", "school"))
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


def test_only_new_activities_reach_the_model(rig: ProjectsRig, source: sqlite3.Connection) -> None:
    rig.service.refresh()
    again = rig.service.refresh()
    assert again["outcome"] == "reused"
    assert rig.sorter.calls == 1
    assert len(rig.events()) == 1

    span(
        source, "2026-09-23T12:00:00", "2026-09-23T12:20:00",
        bundle="com.openai.chat", name="ChatGPT", title="ChatGPT", document="准备这份工作的简历",
    )
    span(source, "2026-09-23T13:00:00", "2026-09-23T13:10:00",
          bundle="com.openai.chat", name="ChatGPT", title="ChatGPT", document="COOP！")
    view = rig.service.refresh()
    assert view["outcome"] == "classified"
    assert rig.sorter.calls == 2
    assert "准备这份工作的简历" in rig.sorter.materials[-1]
    assert "COOP" not in rig.sorter.materials[-1]  # already answered, extended time only
    assert project_of(view, "job-search")["seconds"] == 3000 + 600
    assert view["other"]["seconds"] == 300 + 1200


def test_editing_the_catalog_sorts_everything_again(rig: ProjectsRig) -> None:
    rig.service.refresh()
    assert rig.build(reverse=True).read()["unsorted"]["count"] == 0  # reordering is not an edit
    edited = rig.build(hints="co-op, internships, resumes")
    assert edited.read()["unsorted"]["count"] == 3
    assert edited.refresh()["outcome"] == "classified"
    assert rig.sorter.calls == 2
    # The old catalog's answers still stand for a service built on it.
    assert project_of(rig.service.read(), "job-search")["seconds"] == 3000


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
    rig: ProjectsRig, reply: dict[str, Any], sorted_count: int
) -> None:
    rig.sorter.reply = reply
    view = rig.service.refresh()
    assert view["outcome"] == "classified"
    assert view["unsorted"]["count"] == 3 - sorted_count
    assert sum(len(e["assignments"]) for e in rig.events()) == sorted_count


@pytest.mark.parametrize(
    "bad", [{"groups": "jarvis"}, {"assignments": []}, {"groups": [{"project": "none"}]}]
)
def test_a_malformed_reply_saves_nothing(rig: ProjectsRig, bad: dict[str, Any]) -> None:
    rig.sorter.reply = bad
    view = rig.service.refresh()
    assert view["outcome"] == "failed"
    assert "0 batch(es) saved" in view["error"]
    assert rig.events() == []
    assert view["unsorted"]["count"] == 3


def test_a_later_batch_failing_keeps_the_earlier_ones(
    rig: ProjectsRig, source: sqlite3.Connection
) -> None:
    for n in range(305):  # 308 activities: one batch of 300, then 8
        minute = f"{n // 60 + 1:02d}:{n % 60:02d}"
        span(
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
    assert len(SHORT.findall(rig.sorter.materials[-1])) == 8  # only the leftovers
    assert done["unsorted"]["count"] == 0


def test_provider_failure_and_missing_store(rig: ProjectsRig, tmp_path: Path) -> None:
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


def test_a_refresh_during_a_run_joins_it(rig: ProjectsRig) -> None:
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


def test_http_routes_serve_the_view(rig: ProjectsRig) -> None:
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
        assert project_of(refreshed, "jarvis")["seconds"] == 5400
    bare = InherentDeps(submit_callable=lambda _text: None, broadcaster=InherentBroadcaster())
    with TestClient(create_app(bare)) as client:
        assert client.get("/inherent/projects").status_code == 404


def test_projects_come_from_the_users_settings(tmp_path: Path) -> None:
    shipped = Path("config/jarvis.yaml")
    assert "projects" not in yaml.safe_load(shipped.read_text(encoding="utf-8"))
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "projects:\n  - {id: jarvis, name: Jarvis, repos: [~/Projects/jarvis]}\n",
        encoding="utf-8",
    )
    catalog = parse_catalog(load_runtime_config(shipped, tmp_path).raw["projects"])
    assert [p.id for p in catalog] == ["jarvis"]
    assert all(not r.startswith("~") for p in catalog for r in p.repos)
    cases = (
        [{"id": "Jarvis", "name": "x"}],  # not a lowercase slug
        [{"id": "a", "name": "x"}] * 2,  # repeated id
        [{"id": "none", "name": "x"}],  # reserved for "no project"
    )
    for broken in cases:
        with pytest.raises(ValueError, match="projects entry"):
            parse_catalog(broken)
