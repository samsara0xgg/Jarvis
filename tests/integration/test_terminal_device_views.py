"""ADR 0170 step 4d: the views a brain builds from the owner's TimeSink and repositories.

The moment (what alerts wait for), the job ledger's time column and the projects dashboard read
the device's own files. On a brain they ask the connected terminal, which runs the very reader
code the one-machine daemon runs, over a real socket. The measure each time is the one-machine
answer over the same fixtures; with no terminal the view says the device is not there instead of
showing an empty day, and a brain never reads a file of its own.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Self

import pytest

from jarvis.decision import moment as rules
from jarvis.deployment import bootstrap_runtime
from jarvis.runtime.moment import Moment, MomentSettings
from jarvis.runtime.projects import ProjectsService
from jarvis.runtime.work_state import auto_check
from jarvis.state import device_reads
from jarvis.state.daily_contract import DailyError
from jarvis.state.device_tokens import pair_device
from jarvis.state.projects import Project
from tests.integration.test_projects import NOW, ZONE, Rig, _project, rig, source
from tests.integration.test_terminal_observers import _wait_for
from tests.integration.test_terminal_reads import _Brain, _Terminal
from tests.integration.test_timesink_moment import (
    CHROME,
    T0,
    FakeTimeSink,
    _job_harness,
    _moment_for,
    _zh,
    jev,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.test_job_mail import _Jev

__all__ = ["_zh", "jev", "rig", "source"]

GONE = "/not/on/the/brain"


class _Link:
    """A brain, its hub on a real socket, and a terminal over the device's own files."""

    def __init__(
        self, tmp_path: Path, store: Path | None, repos: tuple[str, ...] = (),
        projects: tuple[Project, ...] = (),
    ) -> None:
        token = pair_device(tmp_path, "macbook")
        log = bootstrap_runtime(tmp_path / "brain-root").event_log
        self.log = log
        self.brain = _Brain(tmp_path, log)
        self.terminal = _Terminal(
            self.brain.url, token, store=store, repos=repos, projects=projects,
        )

    def __enter__(self) -> Self:
        self.brain.__enter__()
        self.terminal.__enter__()
        _wait_for(lambda: bool(self.brain.hub.connected()), "the terminal never connected")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.terminal.stop()
        self.brain.__exit__()

    @property
    def device(self) -> Any:  # noqa: ANN401 — the hub's call, as the runtime binds it.
        return self.brain.hub.call

    def disconnect(self) -> None:
        self.terminal.stop()
        _wait_for(lambda: not self.brain.hub.connected(), "the terminal never left")


# --- the moment -------------------------------------------------------------------------------


def _in_a_meeting(store: FakeTimeSink) -> None:
    store.span(1800, 600, CHROME, "Google Chrome", domain="docs.example", title="Notes")
    store.span(600, 0, CHROME, "Google Chrome", domain="meet.google.com",
               title="Meet - abc-defg-hij", url="https://meet.google.com/abc-defg-hij")


def test_a_brains_moment_is_the_terminals_one_machine_moment_and_holds_for_a_call(
    tmp_path: Path,
) -> None:
    """The facts, the hold and the 现况 doc over the terminal's store equal the one-machine ones."""
    store = FakeTimeSink(tmp_path)
    _in_a_meeting(store)
    db = tmp_path / "memory.db"
    one = _moment_for(store, db)
    with _Link(tmp_path, store.path) as link:
        brain = Moment(MomentSettings(dict.fromkeys(rules.FIELDS, True)), None, db, link.device)
        brain.now = lambda: T0
        facts = brain.facts()
        assert facts == one.facts()
        assert (facts["read"], facts["in_call"], facts["presence"]) == ("ok", "yes", "active")
        assert brain.hold() == one.hold() == "call"
        assert brain.client_hold() == "call"
        assert brain.snapshot() == one.snapshot()

        link.disconnect()
        offline = Moment(MomentSettings(dict.fromkeys(rules.FIELDS, True)), None, db, link.device)
        offline.now = lambda: T0
        unknown = offline.facts()
        assert unknown["read"] == "device_not_connected"
        assert unknown["in_call"] == unknown["presence"] == "unknown"
        assert offline.hold() is None  # unknown holds nothing, as before; the read says why


def test_an_unreadable_store_on_the_terminal_is_unknown_there_too(tmp_path: Path) -> None:
    """A terminal with no TimeSink answers unknown, as a one-machine daemon without one does."""
    with _Link(tmp_path, None) as link:
        brain = Moment(MomentSettings(dict.fromkeys(rules.FIELDS, True)), None,
                       tmp_path / "memory.db", link.device)
        brain.now = lambda: T0
        assert brain.facts()["read"] == "unreadable"


class _Service:
    """What ``auto_check`` refreshes: it only needs to be told."""

    def __init__(self) -> None:
        self.triggers: list[str] = []

    def refresh_in_own_connection(self, *, trigger: str) -> dict[str, Any]:
        self.triggers.append(trigger)
        return {"outcome": "analysed"}


def test_the_work_states_auto_refresh_reads_a_brains_moment_off_the_loop(tmp_path: Path) -> None:
    """``auto_check`` runs on the event loop: a brain's device call must not (it would refuse)."""
    store = FakeTimeSink(tmp_path)
    store.span(60, 0, CHROME, "Google Chrome", domain="docs.example", title="Notes")
    with _Link(tmp_path, store.path) as link:
        moment = Moment(MomentSettings(dict.fromkeys(rules.FIELDS, True)), None,
                        tmp_path / "memory.db", link.device)
        moment.now = lambda: T0
        service = _Service()
        last = asyncio.run(auto_check(service, moment, None, 0.0, 1800.0))  # type: ignore[arg-type]
    assert service.triggers == ["auto"]
    assert last == (("Google Chrome", "docs.example"), 0.0)


# --- the job ledger's time column -------------------------------------------------------------


def test_the_ledger_time_column_is_the_terminals_and_a_missing_terminal_is_said(
    tmp_path: Path, jev: _Jev,
) -> None:
    """Time per company over the terminal's store equals one machine's; offline there is a note."""
    store = FakeTimeSink(tmp_path)
    h = _job_harness(tmp_path, jev, store)
    h.job.poll_once()
    store.span(900, 300, CHROME, "Google Chrome", domain="www.linkedin.com",
               title="Software Developer Co-op - Northwind | LinkedIn",
               url="https://www.linkedin.com/jobs/view/1")
    store.span(160, 100, CHROME, "Google Chrome", domain="acmecorp.greenhouse.io",
               title="Apply now", url="https://boards.greenhouse.io/other/jobs/1")
    want = h.job.ledger()
    northwind = next(g for g in want["ledger"] if g["company"] == "Northwind")
    assert northwind["time_total_s"] == 600
    assert want["job_site_other_s"] == 60
    assert "time_note" not in want

    with _Link(tmp_path / "device", store.path) as link:
        h.job.timesink_path, h.job.device = None, link.device
        assert h.job.ledger() == want
        link.disconnect()
        gone = h.job.ledger()
    assert gone["time_note"] == device_reads.NOT_CONNECTED
    assert all(g["time_spent"] == [] and g["time_total_s"] == 0 for g in gone["ledger"])
    assert [g["company"] for g in gone["ledger"]] == [g["company"] for g in want["ledger"]]


# --- the projects dashboard -------------------------------------------------------------------


def _catalog(repo: str) -> tuple[Project, ...]:
    return (
        Project("job-search", "求职", (), "co-op"),
        Project("jarvis", "Jarvis", (repo,), "Jarvis runtime"),
        Project("school", "学业", (), "UVic"),
    )


def _service(rig_: Rig, log: Path, repo: str, device: Any = None) -> ProjectsService:  # noqa: ANN401
    return ProjectsService(
        event_log_path=log, timesink_path=None if device else rig_.timesink,
        projects=_catalog(repo), sorter=rig_.sorter, model="scripted", tz=ZONE,
        clock=lambda: NOW, device=device,
    )


def test_a_brains_projects_view_is_built_from_the_terminals_store_and_repositories(
    tmp_path: Path, rig: Rig,
) -> None:
    """Week, per-day sums and commits equal the one-machine view; the brain names no path."""
    one = _service(rig, rig.log, str(rig.repo))
    want = one.read()
    jarvis = _project(want, "jarvis")
    assert want["coverage"] == {"timesink": "available", "git": "available"}
    assert want["unsorted"]["count"] == 3
    assert jarvis["commits"]["count"] == 2
    assert "note" not in want

    # The brain's own catalog points at a path that exists nowhere on it: only the terminal's
    # config says where the repository is.
    with _Link(
        tmp_path / "device", rig.timesink, projects=_catalog(str(rig.repo)),
    ) as link:
        brain = _service(rig, rig.log, GONE, link.device)
        assert brain.read() == want

        with pytest.raises(DailyError) as refused:
            device_reads.ask(
                link.device, device_reads.GIT_READ, "project_commits",
                project="not-a-project", since=NOW.isoformat(), until=NOW.isoformat(),
            )
        assert refused.value.code == "not_watched"  # the terminal reads only its own catalog

        link.disconnect()
        gap = brain.read()
    assert gap["coverage"]["timesink"] == "unavailable"
    assert gap["note"] == device_reads.NOT_CONNECTED
    assert gap["unsorted"]["count"] == 0
    assert "device is not connected" in gap["note"]


def test_sorting_with_no_terminal_says_why_and_never_calls_the_model(
    tmp_path: Path, rig: Rig,
) -> None:
    """A refresh with nothing to read is not "no evidence" without a reason."""
    with _Link(tmp_path / "device", rig.timesink) as link:
        brain = _service(rig, rig.log, GONE, link.device)
        link.disconnect()
        result = brain.refresh()
    assert (result["outcome"], result["error"]) == ("no_evidence", device_reads.NOT_CONNECTED)
    assert rig.sorter.calls == 0
