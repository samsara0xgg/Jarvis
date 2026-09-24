"""Opt-in acceptance (ADR 0037): the configured model sorts a real week of TimeSink labels.

--live-llm sends window, page and conversation titles from a read-only copy of
this machine's TimeSink database, with the shipped project catalog, to the
configured work_state preset. Commits are read from the real repositories.
Jarvis state is isolated in a temp event log; nothing is written elsewhere.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.runtime.projects import ProjectsService
from jarvis.runtime.work_state import build_analyst
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.projects import parse_catalog
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

pytestmark = pytest.mark.live_llm

# Labels checked by hand on 2026-09-23 against the day's material; asserted when in the window.
KNOWN = {
    "COOP！": "job-search",
    "分析公司背调岗位和JD": "job-search",
    "Yilun Shi Resume sku.docx": "job-search",
    "cc | jarvis full audit": "jarvis",
    "cc | timesink code review": "timesink",
    "New Tab": None,
}


def _answers(log: Path) -> list[dict[str, Any]]:
    with closing(open_event_log(log)) as conn:
        return [
            a
            for e in iter_events_of_types(conn, ("project.activity_classified",))
            for a in e.payload["assignments"]
        ]


def test_live_sorting_of_a_real_week(tmp_path: Path) -> None:
    """Sort once through HTTP, spot-check known labels, then a second look costs no call."""
    config = yaml.safe_load(Path("config/jarvis.yaml").read_text(encoding="utf-8"))
    source = Path(config["observer"]["timesink"]["db_path"]).expanduser()
    if not source.exists():
        pytest.skip("Real TimeSink database is not available on this host")
    frozen = tmp_path / "timesink.sqlite"
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(frozen)) as dst,
    ):
        src.backup(dst)
    log = tmp_path / "events.sqlite"
    open_event_log(log).close()
    preset = config["work_state"]["preset"]
    sorter = build_analyst(config, preset, pricing_path=None, account_cost=False, kind="projects")
    assert sorter is not None
    service = ProjectsService(
        event_log_path=log,
        timesink_path=frozen,
        projects=parse_catalog(config["projects"]),
        sorter=sorter,
        model=preset,
        tz=ZoneInfo(config["work_state"]["timezone"]),
    )

    async def read() -> dict[str, Any]:
        return service.read()

    async def refresh() -> dict[str, Any]:
        return service.refresh()

    deps = InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        projects_read=read,
        projects_refresh=refresh,
    )
    with TestClient(create_app(deps)) as client:
        before = client.get("/inherent/projects").json()
        view = client.post("/inherent/projects/refresh").json()
        answers = _answers(log)
        again = client.post("/inherent/projects/refresh").json()

    total = before["unsorted"]["seconds"]
    by_label: dict[str, set[str | None]] = {}
    for item in answers:
        by_label.setdefault(item["label"], set()).add(item["project"])
    checked = {label: by_label[label] for label in KNOWN if label in by_label}
    lines = [f"{before['unsorted']['count']} activities, {total / 3600:.1f} h, {view['days']}"]
    lines += [
        f"  {r['name']:<18} {r['seconds'] / 3600:5.1f} h  commits {r['commits']['count']}"
        for r in view["projects"]
    ]
    lines += [
        f"  {'none':<18} {view['other']['seconds'] / 3600:5.1f} h",
        f"  {'unsorted':<18} {view['unsorted']['seconds'] / 3600:5.1f} h",
        f"  spot checks: {checked}",
    ]
    print("\n" + "\n".join(lines))  # noqa: T201 — the live run's evidence, read by a human.
    assert view["outcome"] == "classified", view["error"]
    assert view["unsorted"]["seconds"] <= 0.02 * total
    for label, projects in checked.items():
        assert projects == {KNOWN[label]}, (label, projects)
    # A second look sends only what the first reply left out, never an answered activity.
    later = _answers(log)[len(answers) :]
    assert not {a["key"] for a in answers} & {a["key"] for a in later}
    assert len(later) <= view["unsorted"]["count"]
    if view["unsorted"]["count"] == 0:
        assert again["outcome"] == "reused"
